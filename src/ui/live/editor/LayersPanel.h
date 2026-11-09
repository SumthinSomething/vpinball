// license:GPLv3+

#pragma once

namespace VPX::Layers
{

// The window of the overlay layers: the ball heatmap (show, layer, clear) and the reference image (show, opacity, position, size, rotation). Drawn once per frame by
// the editor; nothing is shown while there is neither a heatmap nor a reference image.
void RenderPanel(float dpi);

// Opens a file picker for the reference image (the image is loaded on a later frame, when the dialog returns)
void BrowseReference();
bool HasReference();
void RemoveReference();

}
