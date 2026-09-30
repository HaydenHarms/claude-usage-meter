"""Builds icon.ico: an XP-style window with a green usage bar."""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))

img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 20, 248, 236), 22, fill="#0831D9")
for y in range(24, 72):  # title bar gradient
    t = (y - 24) / 48
    d.line((12, y, 244, y), fill=(int(0x3D + (0x00 - 0x3D) * t), int(0x95 + (0x55 - 0x95) * t), int(0xFF + (0xE5 - 0xFF) * t)))
d.rectangle((20, 72, 236, 224), fill="#ECE9D8")
d.rounded_rectangle((200, 32, 232, 62), 5, fill="#D8532C", outline="white", width=3)
d.rectangle((36, 110, 220, 170), fill="white", outline="#8E8F8F", width=4)
for x in range(44, 170, 30):
    d.rectangle((x, 118, x + 22, 162), fill="#35CD2E")
img.save(os.path.join(HERE, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
