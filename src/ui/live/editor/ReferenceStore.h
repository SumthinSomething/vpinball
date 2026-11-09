// license:GPLv3+

#pragma once

#include "SketchStore.h"

#include <filesystem>
#include <string>

class PinTable;
class Texture;

// A reference image shown under the parts of the blueprint (a photo of a paper sketch, a whitepaper layout...) so the user and Claude can trace it. The image file
// stays outside of the table (it is not saved in the .vpx); its placement is saved next to the table (<table>.reference.json), in table units.
namespace VPX::Reference
{

struct Placement
{
   std::string path; // image file
   float x = 0.f, y = 0.f; // center on the playfield
   float width = 0.f, height = 0.f; // size on the playfield
   float rotation = 0.f; // degrees, clockwise seen from above
   float opacity = 0.5f; // 0..1
   bool visible = true;
};

class Store
{
public:
   // The store of a table (the live copy of a playtest shares the store of its base table). Loaded from the sidecar file on first use.
   static Store& For(const PinTable* table);

   bool Has() const { return !m_p.path.empty(); }
   Placement m_p; // read and edited directly; call Changed() after editing

   // Loads an image file and places it over the whole table width, centered (the proportions of the image are kept). Returns an error text, empty when OK.
   std::string Load(const std::filesystem::path& file, const PinTable* table);
   // Fits the image to the table width (centered), keeping its proportions
   void FitToTable(const PinTable* table);
   // The texture (loaded when first needed, null if the file can not be read)
   Texture* GetTexture();
   // Pixel size of the image, 0 when it is not loaded
   void ImageSize(unsigned int& w, unsigned int& h);
   // Corners on the playfield: top left, top right, bottom right, bottom left of the image
   void Corners(Sketch::Point out[4]) const;
   void Clear();
   // Saves the placement after it was edited
   void Changed();
   uint64_t Seq() const { return m_seq; }
   const std::filesystem::path& Path() const { return m_sidecar; }

private:
   void LoadSidecar();

   std::filesystem::path m_sidecar;
   Texture* m_texture = nullptr;
   std::string m_texturePath;
   uint64_t m_seq = 0;
};

}
