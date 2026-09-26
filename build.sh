#!/usr/bin/env bash

# 1. Python ki requirements install karega
pip install -r requirements.txt

# 2. FFmpeg ka static version (jo bina install kiye chalta hai) download karega
echo "Downloading FFmpeg..."
wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar -xf ffmpeg-release-amd64-static.tar.xz

# 3. FFmpeg ki main file ko folder mein bahar nikal lega taaki bot isko directly use kar sake
mv ffmpeg-*-amd64-static/ffmpeg ./ffmpeg

# 4. Extra files ko delete karke space free karega
rm -rf ffmpeg-release-amd64-static.tar.xz ffmpeg-*-amd64-static

echo "FFmpeg Installed and Ready!"
