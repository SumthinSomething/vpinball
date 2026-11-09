// license:GPLv3+

#include "core/stdafx.h"
#include "mcp/McpInternal.h"
#include "ui/live/editor/HeatStore.h"
#include "ui/live/editor/ReferenceStore.h"
#include "ui/live/editor/SketchTool.h"

namespace VPX::MCP
{

namespace
{

struct PartBox
{
   IEditable* part = nullptr;
   float x0 = 0, y0 = 0, x1 = 0, y1 = 0;
};

// The parts the ball can touch (the ones with collision shapes: not VR cabinet parts, decals...), with the footprint of their colliders
vector<PartBox> PartBoxes(Player& p)
{
   vector<PartBox> boxes;
   for (const auto& [part, colliders] : CollidersByPart(p))
   {
      if (part == p.m_ptable || part->GetItemType() == eItemPartGroup || part->GetItemType() == eItemBall || part->GetName().empty() || p.m_ptable->IsPlayerSessionObject(part)
         || VPX::Sketch::IsBackdropPart(part, p.m_ptable))
         continue;
      PartBox b;
      b.part = part;
      b.x0 = b.y0 = FLT_MAX;
      b.x1 = b.y1 = -FLT_MAX;
      for (const HitObject* ho : colliders)
      {
         b.x0 = std::min(b.x0, ho->m_hitBBox.left);
         b.x1 = std::max(b.x1, ho->m_hitBBox.right);
         b.y0 = std::min(b.y0, ho->m_hitBBox.top);
         b.y1 = std::max(b.y1, ho->m_hitBBox.bottom);
      }
      if (b.x0 <= b.x1)
         boxes.push_back(b);
   }
   return boxes;
}

json HeatState(const Heat::Store& heat)
{
   return json { { "visible", heat.m_visible }, { "layer", heat.m_layer == Heat::Layer::Time ? "time" : heat.m_layer == Heat::Layer::Impacts ? "impacts" : "resting" }, { "empty", heat.Empty() } };
}

}

void AddLayerTools(std::vector<ToolDef>& tools)
{
   tools.push_back(ToolDef { "vpx_heatmap",
         "The ball heatmap: every vpx_sim run (shot, flip, trace, plunge, autoplay, scenarios...) adds where its balls moved (time), where they sat almost still (resting: the plunger lane wait, stuck, cradled; kept out of the time layer) and where they lost speed hitting something (impacts), to a grid of 20 unit cells over the table. Autoplay records all three. It is drawn over the table in the editor (cold blue to hot red; a Layers panel shows it, hides it and clears it), in vpx_screenshot top and blueprint views (heatmap:false hides it), and read here. "
         "'get' (default) returns the hottest cells of a layer (time: ms of ball time per cell; impacts: speed lost per cell) with the parts nearby each, the total, and per 200 unit band of the table how much of its cells the balls visited (coverage): low coverage in the upper half is dead space nothing sends the ball to (see the no-dead-space rule). "
         "'layer' picks time, impacts or resting for the display and the next get, 'show' / 'hide' the overlay, 'clear' empties it (do it before a new series of runs). The data is not saved with the table.",
         Schema({ { "action", Prop("string", "get (default) | layer | show | hide | clear") }, { "layer", Prop("string", "time | impacts | resting (get, layer)") }, { "top", Prop("integer", "get: hottest cells to return, default 10") } }),
         [](const json& args)
         {
            return WithPlayer([args](Player& p)
            {
               const string action = args.value("action", "get"s);
               Heat::Store& heat = Heat::Store::For(p.m_ptable);
               const string layerName = args.value("layer", ""s);
               if (!layerName.empty())
               {
                  if (layerName != "time" && layerName != "impacts" && layerName != "resting")
                     return ErrorOutput("layer must be time or impacts"s);
                  heat.m_layer = layerName == "time" ? Heat::Layer::Time : layerName == "impacts" ? Heat::Layer::Impacts : Heat::Layer::Resting;
               }
               if (action == "show" || action == "hide")
               {
                  heat.m_visible = action == "show";
                  return TextOutput(HeatState(heat));
               }
               if (action == "clear")
               {
                  heat.Clear();
                  return TextOutput(HeatState(heat));
               }
               if (action == "layer")
                  return TextOutput(HeatState(heat));
               if (action != "get")
                  return ErrorOutput("Unknown action"s);

               json out = HeatState(heat);
               if (heat.Empty())
               {
                  out["note"] = "Nothing recorded yet: run vpx_sim shots, flips or an autoplay in a playtest first";
                  return TextOutput(out);
               }
               const Heat::Layer layer = heat.m_layer;
               const float cell = Heat::Store::CELL;
               const float total = heat.Total(layer), maxValue = heat.Max(layer);
               out["cellSize"] = cell;
               out["total"] = Round(total, 1);
               out["unit"] = layer == Heat::Layer::Time ? "ms of moving ball time" : layer == Heat::Layer::Impacts ? "speed lost" : "ms of ball time at rest";
               out["maxCell"] = Round(maxValue, 1);

               struct Cell { int col, row; float value; };
               vector<Cell> cells;
               for (int row = 0; row < heat.Rows(); row++)
                  for (int col = 0; col < heat.Cols(); col++)
                     if (const float v = heat.Value(layer, col, row); v > 0.f)
                        cells.push_back({ col, row, v });
               std::ranges::sort(cells, [](const Cell& a, const Cell& b) { return a.value > b.value; });
               const vector<PartBox> boxes = PartBoxes(p);
               json hot = json::array();
               for (size_t i = 0; i < cells.size() && i < static_cast<size_t>(std::clamp(args.value("top", 10), 1, 50)); i++)
               {
                  const float cx = heat.Left() + (static_cast<float>(cells[i].col) + 0.5f) * cell, cy = heat.Top() + (static_cast<float>(cells[i].row) + 0.5f) * cell;
                  vector<std::pair<float, string>> nearby;
                  for (const PartBox& b : boxes)
                  {
                     const float dx = std::max({ b.x0 - cx, 0.f, cx - b.x1 }), dy = std::max({ b.y0 - cy, 0.f, cy - b.y1 });
                     if (const float d = std::hypot(dx, dy); d <= 40.f)
                        nearby.emplace_back(d, b.part->GetName());
                  }
                  std::ranges::sort(nearby);
                  json parts = json::array();
                  for (size_t k = 0; k < nearby.size() && k < 3; k++)
                     parts.push_back(nearby[k].second);
                  hot.push_back(json { { "x", Round(cx, 0) }, { "y", Round(cy, 0) }, { "value", Round(cells[i].value, 1) }, { "share", Round(total > 0.f ? cells[i].value / total * 100.f : 0.f, 1) }, { "nearParts", parts } });
               }
               out["hotCells"] = hot;

               // Coverage per band of the table: the share of its cells the balls visited
               json bands = json::array();
               const int bandRows = static_cast<int>(200.f / cell);
               for (int row0 = 0; row0 < heat.Rows(); row0 += bandRows)
               {
                  int visited = 0, count = 0;
                  for (int row = row0; row < std::min(row0 + bandRows, heat.Rows()); row++)
                     for (int col = 0; col < heat.Cols(); col++)
                     {
                        count++;
                        visited += heat.Value(layer, col, row) > 0.f ? 1 : 0;
                     }
                  bands.push_back(json { { "yFrom", Round(heat.Top() + static_cast<float>(row0) * cell, 0) }, { "yTo", Round(heat.Top() + static_cast<float>(std::min(row0 + bandRows, heat.Rows())) * cell, 0) },
                     { "coverage", count > 0 ? Round(100.f * static_cast<float>(visited) / static_cast<float>(count), 0) : 0.0 } });
               }
               out["bandCoverage"] = bands;
               return TextOutput(out);
            });
         } });

   tools.push_back(ToolDef { "vpx_reference",
         "The reference image: a picture (a photo of the user's paper sketch, a whitepaper layout, a theme drawing) shown translucent over the part fills and under the outlines of the blueprint, so you and the user can trace it. The file stays outside the table (it is not saved in the .vpx), its placement is saved next to it. "
         "'set' loads an image file (path: png, jpg, bmp...) and places it over the whole table width, centered, keeping its proportions; with the same call or later, x and y (the center on the playfield, table units), width and height (give one to keep the proportions), rotation (degrees, clockwise seen from above), opacity (0..1), visible and fit (back to the table width) adjust it. 'get' reports the placement, the table size and the pixel size of the image, 'clear' removes it. "
         "To see it use vpx_screenshot view:blueprint (the top view does not draw it); the user can also move, scale, rotate and fade it in the Layers panel. To trace it, read positions off a blueprint screenshot: the image covers table units x 0 to the table width, y 0 to the table height by default.",
         Schema({ { "action", Prop("string", "set (default) | get | clear") }, { "path", Prop("string", "Image file (set)") }, { "x", Prop("number", "Center x on the playfield (set)") }, { "y", Prop("number", "Center y on the playfield (set)") },
                  { "width", Prop("number", "Width on the playfield, table units (set)") }, { "height", Prop("number", "Height on the playfield (set)") }, { "rotation", Prop("number", "Degrees clockwise (set)") },
                  { "opacity", Prop("number", "0.05..1 (set)") }, { "visible", Prop("boolean", "Show or hide (set)") }, { "fit", Prop("boolean", "set: fit to the table width, centered") } }),
         [](const json& args)
         {
            return WithPlayer([args](Player& p)
            {
               const string action = args.value("action", "set"s);
               PinTable* table = p.m_ptable;
               Reference::Store& ref = Reference::Store::For(table);
               const auto state = [&]()
               {
                  unsigned int w = 0, h = 0;
                  ref.ImageSize(w, h);
                  Sketch::Point c[4];
                  ref.Corners(c);
                  return json { { "loaded", ref.Has() }, { "path", ref.m_p.path }, { "x", Round(ref.m_p.x) }, { "y", Round(ref.m_p.y) }, { "width", Round(ref.m_p.width) }, { "height", Round(ref.m_p.height) },
                     { "rotation", Round(ref.m_p.rotation) }, { "opacity", ref.m_p.opacity }, { "visible", ref.m_p.visible }, { "imagePixels", json::array({ w, h }) },
                     { "tableSize", json::array({ Round(table->m_right - table->m_left), Round(table->m_bottom - table->m_top) }) },
                     { "corners", json::array({ json::array({ Round(c[0].x), Round(c[0].y) }), json::array({ Round(c[1].x), Round(c[1].y) }), json::array({ Round(c[2].x), Round(c[2].y) }), json::array({ Round(c[3].x), Round(c[3].y) }) }) },
                     { "note", ref.Has() ? "See it with vpx_screenshot view:blueprint" : "No reference image: set one with path" } };
               };
               if (action == "get")
                  return TextOutput(state());
               if (action == "clear")
               {
                  ref.Clear();
                  return TextOutput(state());
               }
               if (action != "set")
                  return ErrorOutput("Unknown action"s);
               if (args.contains("path") && args["path"].is_string())
               {
                  const string error = ref.Load(PathFromUTF8(args["path"].get<string>()), table);
                  if (!error.empty())
                     return ErrorOutput(error);
               }
               else if (!ref.Has())
                  return ErrorOutput("path is required to load an image first"s);
               Reference::Placement& m = ref.m_p;
               unsigned int w = 0, h = 0;
               ref.ImageSize(w, h);
               const float aspect = w > 0 ? static_cast<float>(h) / static_cast<float>(w) : 1.f;
               if (args.value("fit", false))
                  ref.FitToTable(table);
               if (args.contains("x") && args["x"].is_number())
                  m.x = args["x"].get<float>();
               if (args.contains("y") && args["y"].is_number())
                  m.y = args["y"].get<float>();
               const bool hasW = args.contains("width") && args["width"].is_number() && args["width"].get<float>() > 1.f;
               const bool hasH = args.contains("height") && args["height"].is_number() && args["height"].get<float>() > 1.f;
               if (hasW)
                  m.width = args["width"].get<float>();
               if (hasH)
                  m.height = args["height"].get<float>();
               if (hasW && !hasH)
                  m.height = m.width * aspect;
               else if (hasH && !hasW && aspect > 0.f)
                  m.width = m.height / aspect;
               if (args.contains("rotation") && args["rotation"].is_number())
                  m.rotation = args["rotation"].get<float>();
               if (args.contains("opacity") && args["opacity"].is_number())
                  m.opacity = std::clamp(args["opacity"].get<float>(), 0.05f, 1.f);
               if (args.contains("visible") && args["visible"].is_boolean())
                  m.visible = args["visible"].get<bool>();
               ref.Changed();
               return TextOutput(state());
            });
         } });
}

}
