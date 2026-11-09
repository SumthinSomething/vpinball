// license:GPLv3+

#include "core/stdafx.h"
#include "ui/live/editor/LockStore.h"
#include "core/ieditable.h"
#include "parts/Collection.h"
#include "parts/PartGroup.h"
#include "parts/pintable.h"

#include <nlohmann/json.hpp>

#include <fstream>
#include <map>
#include <memory>
#include <sstream>

namespace VPX::Locks
{

using json = nlohmann::json;

namespace
{

bool Contains(const std::vector<std::string>& names, const std::string& name)
{
   return std::ranges::any_of(names, [&name](const std::string& n) { return StrCompareNoCase(n, name); });
}

void SetIn(std::vector<std::string>& names, const std::string& name, bool locked)
{
   if (locked)
   {
      if (!Contains(names, name))
         names.push_back(name);
   }
   else
      std::erase_if(names, [&name](const std::string& n) { return StrCompareNoCase(n, name); });
}

}

Store& Store::For(const PinTable* table)
{
   static std::map<std::string, std::unique_ptr<Store>> stores;
   if (table != nullptr && table->m_liveBaseTable != nullptr)
      table = table->m_liveBaseTable;
   std::filesystem::path path;
   std::string key;
   if (table != nullptr && !table->m_filename.empty())
   {
      path = table->m_filename;
      path.replace_extension(".locks.json");
      key = path.string();
   }
   else
      key = std::format("memory:{}", static_cast<const void*>(table));
   auto it = stores.find(key);
   if (it == stores.end())
   {
      it = stores.emplace(key, std::make_unique<Store>()).first;
      it->second->m_path = path;
      it->second->Load();
   }
   return *it->second;
}

void Store::Load()
{
   m_parts.clear();
   m_collections.clear();
   if (m_path.empty() || !std::filesystem::exists(m_path))
      return;
   try
   {
      std::ifstream in(m_path, std::ios::binary);
      std::stringstream ss;
      ss << in.rdbuf();
      const json doc = json::parse(ss.str(), nullptr, false);
      if (!doc.is_object())
      {
         PLOGE << "Locks file is not valid JSON: " << m_path.string();
         return;
      }
      for (const auto& [key, target] : { std::pair<const char*, std::vector<std::string>*> { "parts", &m_parts }, { "collections", &m_collections } })
         if (doc.contains(key) && doc[key].is_array())
            for (const json& e : doc[key])
               if (e.is_string())
                  SetIn(*target, e.get<std::string>(), true);
   }
   catch (const std::exception& e)
   {
      PLOGE << "Locks could not be read: " << e.what();
   }
}

void Store::Save()
{
   ++m_seq;
   if (m_path.empty())
      return;
   try
   {
      // No locks left: the file goes away
      if (Empty())
      {
         std::error_code ec;
         std::filesystem::remove(m_path, ec);
         return;
      }
      const json doc { { "version", 1 }, { "parts", m_parts }, { "collections", m_collections } };
      const std::filesystem::path tmp = m_path.string() + ".tmp";
      {
         std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
         out << doc.dump(1, '\t');
         if (!out)
         {
            PLOGE << "Locks could not be written: " << tmp.string();
            return;
         }
      }
      std::filesystem::rename(tmp, m_path);
   }
   catch (const std::exception& e)
   {
      PLOGE << "Locks could not be saved: " << e.what();
   }
}

bool Store::IsPartNameLocked(const std::string& name) const { return Contains(m_parts, name); }

bool Store::IsCollectionNameLocked(const std::string& name) const { return Contains(m_collections, name); }

void Store::SetPart(const std::string& name, bool locked)
{
   if (IsPartNameLocked(name) == locked)
      return;
   SetIn(m_parts, name, locked);
   Save();
}

void Store::SetCollection(const std::string& name, bool locked)
{
   if (IsCollectionNameLocked(name) == locked)
      return;
   SetIn(m_collections, name, locked);
   Save();
}

std::string Store::Reason(const IEditable* part, const PinTable* table) const
{
   if (part == nullptr || Empty())
      return {};
   if (IsPartNameLocked(part->GetName()))
      return "the part itself is locked";
   for (const PartGroup* g = part->GetPartGroup(); g != nullptr; g = g->GetPartGroup())
      if (IsPartNameLocked(g->GetName()))
         return "its part group '" + g->GetName() + "' is locked";
   if (table != nullptr && !m_collections.empty())
      for (CComObject<Collection>* c : table->GetCollections())
         if (IsCollectionNameLocked(c->m_name) && std::ranges::find(c->GetParts(), part) != c->GetParts().end())
            return "it is in the locked collection '" + c->m_name + "'";
   return {};
}

}
