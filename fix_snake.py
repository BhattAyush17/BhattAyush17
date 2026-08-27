import re

def hex_to_gray(match):
    color = match.group(0)
    if color.lower() in ["#0d1117", "#161b22", "#000000", "#ffffff"]:
        return color
    hex_color = color.lstrip('#')
    if len(hex_color) == 6:
        r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        # if it's purple (the snake color)
        if r > 100 and g < 100 and b > 100:
            return "#ffffff" # Make snake white
        gray = int(0.299 * r + 0.587 * g + 0.114 * b)
        return f"#{gray:02x}{gray:02x}{gray:02x}"
    return color

with open("assets/snake/github-contribution-grid-snake-dark.svg", "r") as f:
    content = f.read()

new_content = re.sub(r'#[0-9a-fA-F]{6}', hex_to_gray, content)

with open("assets/snake/github-contribution-grid-snake-dark.svg", "w") as f:
    f.write(new_content)

print("Fixed snake SVG colors")
