#pragma once
#include <windows.h>
#include <gdiplus.h>
#include <memory>

// Captures one Max-owned window's client pixels, never the composed desktop.
// Callers retain ownership/view guards and validate dimensions after capture.
namespace WindowCapture {
struct Result {
    std::unique_ptr<Gdiplus::Bitmap> bitmap;
    const char* method;
};
Result Client(HWND window);
// Core shutdown: releases the activation factories C++/WinRT cached for
// capture. The cache lives in this module and has no destructor, so without
// this every core unload would leak the OS factory references it holds.
void ReleaseFactories() noexcept;
}
