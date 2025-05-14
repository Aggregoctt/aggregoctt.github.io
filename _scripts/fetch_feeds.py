import os
import base64
import hashlib
import feedparser
import newspaper
import requests
from datetime import datetime
from slugify import slugify
import yaml
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from PIL import Image
from io import BytesIO

OUTPUT_DIR = "_posts/feeds"
MEDIA_DIR = "assets/media"
SEEN_FILE = "seen_urls.txt"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

IMAGE_FORMAT = "WEBP"
IMAGE_WIDTH = 1000
IMAGE_QUALITY = 60

with open("_data/sources.yml", "r", encoding="utf-8") as f:
    feeds = yaml.safe_load(f)

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

seen = set()
new_seen = []
if os.path.exists(SEEN_FILE):
    with open(SEEN_FILE, "r") as f:
        seen = set(f.read().splitlines())

def hashed_id(id:str) -> str:
    #return hashlib.md5(id.encode()).hexdigest()[:16]
    return base64.urlsafe_b64encode(hashlib.md5(id.encode()).digest()).decode().strip("=")

def html_soup(html:str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")

def http_request(url:str) -> requests.get:
    r = requests.get(url, timeout=10, headers={"user-agent": USER_AGENT})
    r.raise_for_status()
    return r

def get_mime_type(headers:dict) -> str:
    return headers["content-type"].split(";")[0]

def compress_image(image:bytes, output_path:str) -> bool:
    try:
        image = Image.open(BytesIO(image))
        image_format = image.format
        if image_format in ["JPEG", "JPG", "WEBP", "PNG", "AVIF"]: # do NOT convert GIFs
            if image.width > IMAGE_WIDTH:
                ratio = IMAGE_WIDTH / float(image.width)
                new_height = int(image.height * ratio)
                image = image.resize((IMAGE_WIDTH, new_height), Image.LANCZOS)
            image = image.convert("RGB") # to ensure compatibility
            image.save(f"{os.path.splitext(output_path)[0]}.{IMAGE_FORMAT.lower()}", format=IMAGE_FORMAT, quality=IMAGE_QUALITY, optimize=True)
            return True
    except Exception as e:
        print(f"Error compressing image {url}: {e}")
    return False

def cache_image(url:str, post_id:str) -> str:
    r = http_request(url)
    ext = os.path.splitext(urlparse(url).path)[1].strip('.') or get_mime_type(r.headers).split("/")[1]
    img_name = f"{post_id}-{hashed_id(url)}.{ext}"
    local_path = os.path.join(MEDIA_DIR, img_name)
    if compress_image(r.content, local_path):
        img_name = f"{os.path.splitext(img_name)[0]}.{IMAGE_FORMAT.lower()}"
    else:
        with open(local_path, "wb") as f:
            f.write(r.content)
    print(f"✓ Cached: {img_name}")
    return f"{MEDIA_DIR}/{img_name}"

def download_media_and_replace(html:str, base_url:str, post_id:str) -> list[str, str]:
    soup = html_soup(html)
    first_img = None
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        abs_url = urljoin(base_url, src)
        try:
            src_new = cache_image(abs_url, post_id)
            if not first_img:
                first_img = src_new
            img["src"] = f"/{src_new}"
        except Exception as e:
            print(f"Failed to download {abs_url}: {e}")
            return None
    return [str(soup), first_img]

def make_filename(title:str, date:datetime, link:str) -> str:
    slug = slugify(title or "untitled")[:50]
    return f"{date.strftime('%Y-%m-%d')}-{slug}-{hashed_id(link)}.html"

# validate feeds; if fields are missing, this will throw
for feed in feeds:
    feed = feeds[feed]
    feed["url"], feed["category"]

for feed in feeds:
    feed_id = feed
    feed = feeds[feed_id]
    feed_url = feed["url"]
    category = feed["category"]
    print(f"Fetching: {feed_url}")
    d = feedparser.parse(feed_url)

    for entry in d.entries:
        link = entry.get("link")
        guid = entry.get("id")
        if not link or (link in seen) or (guid and guid in seen):
            continue

        date_struct = (
            entry.get("published_parsed") or
            entry.get("updated_parsed") or
            datetime.utcnow().timetuple()
        )
        date = datetime(*date_struct[:6])
        title = entry.get("title", "Untitled")
        post_id = hashed_id(link)
        filename = make_filename(title, date, link)

        output_dir = os.path.join(OUTPUT_DIR, feed_id)
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)

        summary = entry.get('summary', '')
        content_html = (entry.get("content", [{}])[0].get("value") or summary)

        result = download_media_and_replace(content_html, link, post_id)
        if result:
            [content_html, cover_img] = result
        else:
            continue

        if not cover_img:
            try:
                r = http_request(link)
                if get_mime_type(r.headers) == "text/html":
                    cover_imgs = html_soup(r.content).select('meta[property="og:image"]')
                    if len(cover_imgs):
                        cover_url = cover_imgs[0].get("content") or None
                        if cover_url:
                            try:
                                cover_img = cache_image(cover_url, post_id)
                            except Exception as e:
                                print(f"Failed to download {cover_url}: {e}")
                                continue
            except Exception as e:
                print(f"Failed to download {link}: {e}")
                continue

        try:
            article_html = newspaper.article(link).article_html
            if article_html and article_html.strip():
                content_html = article_html
        except Exception as e:
            print(f"Failed to parse {link}: {e}")
            if type(e) != newspaper.ArticleBinaryDataException:
                continue

        tags = []
        if "tags" in entry:
            tags = [t["term"] for t in entry.tags if "term" in t]

        front_matter = {
            "title": title,
            "date": date.isoformat(),
            "image": cover_img,
            "canonical_url": link,
            "tags": tags,
            "author": entry.get("author"),
            "source": feed_id,
            "excerpt": summary.strip(),
        }

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("---\n")
            yaml.dump(front_matter, f, allow_unicode=True)
            f.write("---\n")
            f.write(content_html)

        new_seen.append(link)
        if guid and guid != link:
            new_seen.append(guid)
        print(f"✓ Saved: {filename}")

# Save new seen URLs
with open(SEEN_FILE, "a") as f:
    for url in new_seen:
        f.write(f"{url}\n")
