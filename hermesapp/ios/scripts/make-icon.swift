#!/usr/bin/env swift
// Imports the native Hermes desktop artwork; iOS applies its own corner mask.
// Usage (from ios/): swift scripts/make-icon.swift /path/to/hermes-agent/apps/desktop/assets/icon.png
import AppKit
import UniformTypeIdentifiers

let size = 1024
guard CommandLine.arguments.count == 2 else {
    fatalError("Pass the native Hermes desktop assets/icon.png path.")
}
let sourceURL = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: "HermesVoice/Assets.xcassets/AppIcon.appiconset/AppIcon.png")
guard let source = CGImageSourceCreateWithURL(sourceURL as CFURL, nil),
      let artwork = CGImageSourceCreateImageAtIndex(source, 0, nil),
      artwork.width == size, artwork.height == size else {
    fatalError("Expected the 1024×1024 Hermes desktop PNG.")
}
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
guard let context = CGContext(
    data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
    space: colorSpace, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
) else { fatalError("Cannot create icon bitmap") }
// App Store icons must be opaque. Preserve the original artwork and fill only its
// transparent desktop padding with white, matching the native icon's background.
let rect = CGRect(x: 0, y: 0, width: size, height: size)
context.setFillColor(CGColor(gray: 1, alpha: 1))
context.fill(rect)
context.draw(artwork, in: rect)
guard let image = context.makeImage(),
      let destination = CGImageDestinationCreateWithURL(output as CFURL, UTType.png.identifier as CFString, 1, nil)
else { fatalError("Cannot encode icon") }
CGImageDestinationAddImage(destination, image, nil)
guard CGImageDestinationFinalize(destination) else { fatalError("Cannot write icon") }
print("Imported native Hermes icon into \(output.path)")
