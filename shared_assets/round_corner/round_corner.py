#!/usr/bin/env python3.11
"""
Round Corner Image Processor
Takes any image and applies rounded corners with high border radius.
Works with rectangular and non-rectangular images.
"""

from PIL import Image, ImageDraw
import os
import sys

def round_corners(input_path, output_path, radius_percent=25):
    """
    Apply rounded corners to an image.

    Args:
        input_path: Path to input image
        output_path: Path to save output image
        radius_percent: Corner radius as percentage of shortest side (default 25%)
    """
    # Open the image
    img = Image.open(input_path)

    # Convert to RGBA to handle transparency
    if img.mode != 'RGBA':
        img = img.convert('RGBA')

    # Calculate radius based on image size
    width, height = img.size
    radius = int(min(width, height) * (radius_percent / 100))

    # Create a mask for rounded corners
    mask = Image.new('L', (width, height), 0)
    draw = ImageDraw.Draw(mask)

    # Draw rounded rectangle on mask
    draw.rounded_rectangle(
        [(0, 0), (width, height)],
        radius=radius,
        fill=255
    )

    # If the original image has transparency, combine it with our mask
    if img.mode == 'RGBA':
        # Get the alpha channel of the original image
        _, _, _, original_alpha = img.split()

        # Combine the rounded corner mask with the original alpha
        # This preserves transparency in the original image
        combined_mask = Image.composite(mask, Image.new('L', (width, height), 0), original_alpha)

        # Apply the combined mask
        img.putalpha(combined_mask)
    else:
        # No original alpha, just use our rounded corner mask
        img.putalpha(mask)

    # Save the result
    img.save(output_path, 'PNG')
    print(f"Processed: {os.path.basename(input_path)} -> {os.path.basename(output_path)}")

def process_directory(input_dir, output_dir, radius_percent=25):
    """
    Process all images in input directory.
    """
    # Supported image formats
    supported_formats = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp')

    # Get all image files from input directory
    image_files = [f for f in os.listdir(input_dir)
                   if f.lower().endswith(supported_formats)]

    if not image_files:
        print(f"No images found in {input_dir}")
        return

    print(f"Found {len(image_files)} image(s) to process")
    print(f"Border radius: {radius_percent}% of shortest side\n")

    # Process each image
    for img_file in image_files:
        input_path = os.path.join(input_dir, img_file)

        # Create output filename (preserve name, change extension to .png)
        base_name = os.path.splitext(img_file)[0]
        output_path = os.path.join(output_dir, f"{base_name}_rounded.png")

        try:
            round_corners(input_path, output_path, radius_percent)
        except Exception as e:
            print(f"Error processing {img_file}: {str(e)}")

def main():
    # Get the script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_dir = os.path.join(script_dir, 'input')
    output_dir = os.path.join(script_dir, 'output')

    # Check if input directory exists and has files
    if not os.path.exists(input_dir):
        print(f"Error: Input directory not found: {input_dir}")
        sys.exit(1)

    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)

    # Get radius from command line argument or use default
    radius_percent = 25  # Default: 25% for a high border radius
    if len(sys.argv) > 1:
        try:
            radius_percent = float(sys.argv[1])
            if radius_percent < 0 or radius_percent > 50:
                print("Warning: Radius should be between 0 and 50, using default 25%")
                radius_percent = 25
        except ValueError:
            print("Warning: Invalid radius value, using default 25%")

    # Process all images
    process_directory(input_dir, output_dir, radius_percent)
    print("\nDone! Check the output folder for results.")

if __name__ == "__main__":
    main()
