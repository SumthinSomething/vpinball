// license:GPLv3+

#include "core/stdafx.h"
#include "LayersPanel.h"
#include "HeatStore.h"
#include "ReferenceStore.h"
#include "core/player.h"
#include "parts/pintable.h"
#include "renderer/Window.h"
#include "ui/live/FileDialog.h"
#include "mcp/McpBookmarks.h"

namespace VPX::Layers
{

namespace
{
// The file picked in the dialog (the callback runs on another thread: it only stores the path, the image is loaded by the next frame of the panel)
std::shared_ptr<std::string> g_picked = std::make_shared<std::string>();
std::string g_error; // why the last image could not be loaded, shown until dismissed
}

void BrowseReference()
{
   if (g_pplayer == nullptr || g_pplayer->m_ptable == nullptr)
      return;
   // Start in the folder of the current image, else of the table
   std::string location;
   const Reference::Store& reference = Reference::Store::For(g_pplayer->m_ptable);
   if (reference.Has())
      location = PathToUTF8(PathFromUTF8(reference.m_p.path).parent_path());
   else if (!g_pplayer->m_ptable->m_filename.empty())
      location = PathToUTF8(g_pplayer->m_ptable->m_filename.parent_path());
   static const SDL_DialogFileFilter filters[] = { { "Images", "png;jpg;jpeg;bmp;webp;tga" }, { "All files", "*" } };
   ShowOpenFileDialog(
      [](void* userdata, const char* const* filelist, int)
      {
         auto* res = static_cast<std::shared_ptr<std::string>*>(userdata);
         if (filelist != nullptr && filelist[0] != nullptr)
            **res = filelist[0];
         delete res;
      },
      new std::shared_ptr<std::string>(g_picked), g_pplayer->m_playfieldWnd->GetCore(), filters, 2, location.empty() ? nullptr : location.c_str(), false);
}

bool HasReference() { return g_pplayer != nullptr && g_pplayer->m_ptable != nullptr && Reference::Store::For(g_pplayer->m_ptable).Has(); }

void RemoveReference()
{
   if (g_pplayer != nullptr && g_pplayer->m_ptable != nullptr)
      Reference::Store::For(g_pplayer->m_ptable).Clear();
}

void RenderPanel(float dpi)
{
   if (g_pplayer == nullptr || g_pplayer->m_ptable == nullptr)
      return;
   PinTable* table = g_pplayer->m_ptable;
   Heat::Store& heat = Heat::Store::For(table);
   Reference::Store& reference = Reference::Store::For(table);
   MCP::Bookmarks::Store& bookmarks = MCP::Bookmarks::Store::For(table);

   // The image picked in the file dialog
   if (!g_picked->empty())
   {
      const std::string path = *g_picked;
      g_picked->clear();
      g_error = reference.Load(PathFromUTF8(path), table);
   }

   if (heat.Empty() && !reference.Has() && g_error.empty() && bookmarks.All().empty())
      return;

   ImGui::SetNextWindowPos(ImVec2(20.f * dpi, 120.f * dpi), ImGuiCond_FirstUseEver);
   ImGui::SetNextWindowBgAlpha(0.95f);
   constexpr ImGuiWindowFlags flags = ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoFocusOnAppearing;
   if (ImGui::Begin("Layers###ClaudeLayers", nullptr, flags))
   {
      if (!g_error.empty())
      {
         ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + 320.f * dpi);
         ImGui::TextColored(ImVec4(1.f, 0.45f, 0.4f, 1.f), "%s", g_error.c_str());
         ImGui::PopTextWrapPos();
         if (ImGui::Button("Dismiss"))
            g_error.clear();
         ImGui::SameLine();
         if (ImGui::Button("Try another image..."))
            BrowseReference();
      }
      if (!heat.Empty() && ImGui::CollapsingHeader("Ball heatmap", ImGuiTreeNodeFlags_DefaultOpen))
      {
         ImGui::Checkbox("Show##heat", &heat.m_visible);
         int layer = heat.m_layer == Heat::Layer::Time ? 0 : heat.m_layer == Heat::Layer::Impacts ? 1 : 2;
         if (ImGui::RadioButton("Time spent", &layer, 0))
            heat.m_layer = Heat::Layer::Time;
         ImGui::SameLine();
         if (ImGui::RadioButton("Hits", &layer, 1))
            heat.m_layer = Heat::Layer::Impacts;
         if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Where the ball lost speed hitting something (sum of the speed lost per cell)");
         ImGui::SameLine();
         if (ImGui::RadioButton("Resting", &layer, 2))
            heat.m_layer = Heat::Layer::Resting;
         if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Where the ball sat almost still (waiting in the plunger lane, stuck, cradled): kept out of 'Time spent'");
         heat.m_layer = layer == 0 ? Heat::Layer::Time : layer == 1 ? Heat::Layer::Impacts : Heat::Layer::Resting;
         ImGui::TextDisabled("%.1f s moving, %.1f s resting (simulator runs)", heat.Total(Heat::Layer::Time) / 1000.f, heat.Total(Heat::Layer::Resting) / 1000.f);
         if (ImGui::Button("Clear heatmap"))
            heat.Clear();
      }
      if (!bookmarks.All().empty() && ImGui::CollapsingHeader("Play bookmarks (F8)", ImGuiTreeNodeFlags_DefaultOpen))
      {
         // The moments the user marked as wrong while playing: show the path of the balls over the table, or delete them
         int removeId = 0;
         for (const MCP::Bookmarks::Bookmark& listed : bookmarks.All())
         {
            MCP::Bookmarks::Bookmark* b = bookmarks.Find(listed.id);
            if (b == nullptr)
               continue;
            ImGui::PushID(b->id);
            ImGui::Checkbox("##showbm", &b->visible);
            if (ImGui::IsItemHovered())
               ImGui::SetTooltip("Show the path of the balls over the table");
            ImGui::SameLine();
            ImGui::TextColored(ImGui::ColorConvertU32ToFloat4(MCP::Bookmarks::BookmarkColor(b->id)), "#%d", b->id);
            ImGui::SameLine();
            const float seconds = b->frames.empty() ? 0.f : static_cast<float>(b->frames.back().t - b->frames.front().t) / 1000.f;
            ImGui::TextDisabled("%.1f s%s%s", seconds, b->note.empty() ? "" : ": ", b->note.c_str());
            ImGui::SameLine();
            if (ImGui::SmallButton("Delete"))
               removeId = b->id;
            ImGui::PopID();
         }
         if (removeId != 0)
            bookmarks.Remove(removeId);
         if (!bookmarks.All().empty() && ImGui::Button("Clear bookmarks"))
            bookmarks.Clear();
         ImGui::TextDisabled("Press F8 while playing to keep the last 6 seconds");
      }
      if (reference.Has() && ImGui::CollapsingHeader("Reference image", ImGuiTreeNodeFlags_DefaultOpen))
      {
         Reference::Placement& m = reference.m_p;
         bool changed = false;
         ImGui::TextDisabled("%s", PathToUTF8(PathFromUTF8(m.path).filename()).c_str());
         changed |= ImGui::Checkbox("Show##ref", &m.visible);
         ImGui::SetNextItemWidth(160.f * dpi);
         changed |= ImGui::SliderFloat("Opacity", &m.opacity, 0.05f, 1.f, "%.2f");
         ImGui::SetNextItemWidth(160.f * dpi);
         changed |= ImGui::DragFloat("X", &m.x, 1.f, -2000.f, 3000.f, "%.0f");
         ImGui::SetNextItemWidth(160.f * dpi);
         changed |= ImGui::DragFloat("Y", &m.y, 1.f, -2000.f, 5000.f, "%.0f");
         {
            // The proportions of the image are kept when the width changes
            float width = m.width;
            ImGui::SetNextItemWidth(160.f * dpi);
            if (ImGui::DragFloat("Width", &width, 1.f, 20.f, 6000.f, "%.0f") && width > 1.f && m.width > 1.f)
            {
               m.height *= width / m.width;
               m.width = width;
               changed = true;
            }
         }
         ImGui::SetNextItemWidth(160.f * dpi);
         changed |= ImGui::DragFloat("Rotation", &m.rotation, 0.2f, -180.f, 180.f, "%.1f deg");
         if (ImGui::Button("Fit to table"))
         {
            reference.FitToTable(table);
            changed = true;
         }
         ImGui::SameLine();
         if (ImGui::Button("Load another..."))
            BrowseReference();
         ImGui::SameLine();
         if (ImGui::Button("Remove"))
         {
            reference.Clear();
            changed = false;
         }
         if (changed)
            reference.Changed();
      }
   }
   ImGui::End();
}

}
