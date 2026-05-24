import requests
from bs4 import BeautifulSoup

url = "https://github.com/BhattAyush17"
headers = {"User-Agent": "Mozilla/5.0"}
res = requests.get(url, headers=headers)
soup = BeautifulSoup(res.text, 'html.parser')

print("=== Badges / Achievements ===")
achievements = soup.find_all("img", class_="achievement-badge-card")
for badge in achievements:
    print(badge.get("alt"), badge.get("src"))

# Also try selector
print("\n=== Try select ===")
for element in soup.select("img[src*='achievement']"):
    print(element.get("alt"), element.get("src"))
