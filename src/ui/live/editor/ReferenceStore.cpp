// license:GPLv3+

#include "core/stdafx.h"
#include "ui/live/editor/ReferenceStore.h"
#include "parts/pintable.h"
#include "renderer/Texture.h"

#include <nlohmann/json.hpp>

#include <cmath>
#include <fstream>
#include <map>
#include <memory>
#include <sstream>

namespace VPX::Reference
{

using json = nlohmann::json;

namespace
{
// Textures are never freed: the texture manager of the renderer may still hold a replaced or cleared image by its address, and a reference image is small next to a table
std::vector<Texture*>& Graveyard()
{
   static std::vector<Texture*> graveyard;
   return graveyard;
}
}

Store& Store::For(const PinTable* table)
{
   static std::map<std::string, std::unique_ptr<Store>> stores;
   if (table != nullptr && table->m_liveBaseTable != nullptr)
      table = table->m_liveBaseTable;
   std::filesystem::path sidecar;
   std::string key;
   if (table != nullptr && !table->m_filename.empty())
   {
      sidecar = table->m_filename;
      sidecar.replace_extension(".reference.json");
      key = sidecar.string();
   }
   else
      key = std::format("memory:{}", static_cast<const void*>(table));
   auto it = stores.find(key);
   if (it == stores.end())
   {
      it = stores.emplace(key, std::make_unique<Store>()).first;
      it->second->m_sidecar = sidecar;
      it->second->LoadSidecar();
   }
   return *it->second;
}

void Store::LoadSidecar()
{
   if (m_sidecar.empty() || !std::filesystem::exists(m_sidecar))
      return;
   try
   {
      std::ifstream in(m_sidecar, std::ios::binary);
      std::stringstream ss;
      ss << in.rdbuf();
      const json doc = json::parse(ss.str(), nullptr, false);
      if (!doc.is_object() || !doc.contains("path") || !doc["path"].is_string())
         return;
      m_p.path = doc["path"].get<std::string>();
      m_p.x = doc.value("x", 0.f);
      m_p.y = doc.value("y", 0.f);
      m_p.width = doc.value("width", 0.f);
      m_p.height = doc.value("height", 0.f);
      m_p.rotation = doc.value("rotation", 0.f);
      m_p.opacity = std::clamp(doc.value("opacity", 0.5f), 0.f, 1.f);
      m_p.visible = doc.value("visible", true);
   }
   catch (const std::exception& e)
   {
      PLOGE << "Reference image placement could not be read: " << e.what();
   }
}

void Store::Changed()
{
   ++m_seq;
   if (m_sidecar.empty())
      return;
   try
   {
      if (!Has())
      {
         std::error_code ec;
         std::filesystem::remove(m_sidecar, ec);
         return;
      }
      const json doc { { "version", 1 }, { "path", m_p.path }, { "x", m_p.x }, { "y", m_p.y }, { "width", m_p.width }, { "height", m_p.height }, { "rotation", m_p.rotation },
         { "opacity", m_p.opacity }, { "visible", m_p.visible } };
      const std::filesystem::path tmp = m_sidecar.string() + ".tmp";
      {
         std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
         out << doc.dump(1, '\t');
      }
      std::filesystem::rename(tmp, m_sidecar);
   }
   catch (const std::exception& e)
   {
      PLOGE << "Reference image placement could not be saved: " << e.what();
   }
}

Texture* Store::GetTexture()
{
   if (!Has())
      return nullptr;
   if (m_texture != nullptr && m_texturePath == m_p.path)
      return m_texture;
   if (m_texture != nullptr)
      Graveyard().push_back(m_texture);
   m_texture = nullptr;
   m_texturePath = m_p.path;
   if (!std::filesystem::exists(PathFromUTF8(m_p.path)))
      return nullptr;
   m_texture = Texture::CreateFromFile(PathFromUTF8(m_p.path));
   return m_texture;
}

void Store::ImageSize(unsigned int& w, unsigned int& h)
{
   w = h = 0;
   if (Texture* t = GetTexture())
   {
      w = t->m_width;
      h = t->m_height;
   }
}

void Store::FitToTable(const PinTable* table)
{
   unsigned int w = 0, h = 0;
   ImageSize(w, h);
   if (w == 0 || h == 0 || table == nullptr)
      return;
   m_p.width = table->m_right - table->m_left;
   m_p.height = m_p.width * static_cast<float>(h) / static_cast<float>(w);
   m_p.x = (table->m_left + table->m_right) * 0.5f;
   m_p.y = (table->m_top + table->m_bottom) * 0.5f;
   m_p.rotation = 0.f;
}

std::string Store::Load(const std::filesystem::path& file, const PinTable* table)
{
   if (!std::filesystem::exists(file))
      return "The file does not exist: " + file.string();
   Placement old = m_p;
   m_p.path = PathToUTF8(file);
   if (GetTexture() == nullptr)
   {
      m_p = old;
      return "The image could not be read (png, jpg, bmp, webp...): " + file.string();
   }
   FitToTable(table);
   m_p.visible = true;
   Changed();
   return {};
}

void Store::Corners(Sketch::Point out[4]) const
{
   const float hw = m_p.width * 0.5f, hh = m_p.height * 0.5f;
   const float rad = m_p.rotation * 3.14159265f / 180.f, c = std::cos(rad), s = std::sin(rad);
   const float local[4][2] = { { -hw, -hh }, { hw, -hh }, { hw, hh }, { -hw, hh } };
   for (int i = 0; i < 4; i++)
      out[i] = { m_p.x + local[i][0] * c - local[i][1] * s, m_p.y + local[i][0] * s + local[i][1] * c };
}

void Store::Clear()
{
   if (m_texture != nullptr)
      Graveyard().push_back(m_texture);
   m_texture = nullptr;
   m_texturePath.clear();
   m_p = Placement();
   Changed();
}

}
