import os
import base64
import hashlib
import feedparser
import newspaper
import requests
import yaml
from datetime import datetime
from slugify import slugify
import multiprocessing as mp
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from PIL import Image, ImageFile
from io import BytesIO

OUTPUT_DIR = "_posts/feeds"
MEDIA_DIR = "assets/media"
SEEN_FILE = "seen_urls.txt"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

IMAGE_FORMAT = "JPEG"
IMAGE_WIDTH = 1000
IMAGE_QUALITY = 75

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

def resize_gif(image:bytes, save_as:str, resize_to=None):
    all_frames = extract_and_resize_frames(image, resize_to)
    if len(all_frames) == 1:
        print("Warning: only 1 frame found")
        all_frames[0].save(save_as, optimize=True)
    else:
        all_frames[0].save(save_as, optimize=True, save_all=True, append_images=all_frames[1:], loop=1000)

def analyseImage(image:bytes):
    im = Image.open(BytesIO(image))
    results = {
        'size': im.size,
        'mode': 'full',
    }
    try:
        while True:
            if im.tile:
                tile = im.tile[0]
                update_region = tile[1]
                update_region_dimensions = update_region[2:]
                if update_region_dimensions != im.size:
                    results['mode'] = 'partial'
                    break
            im.seek(im.tell() + 1)
    except EOFError:
        pass
    return results

def extract_and_resize_frames(image:bytes, resize_to=None):
    mode = analyseImage(image)['mode']
    im = Image.open(BytesIO(image))

    if not resize_to:
        resize_to = (im.size[0] // 2, im.size[1] // 2)

    i = 0
    p = im.getpalette()
    last_frame = im.convert('RGBA')
    all_frames = []

    try:
        while True:
            if not im.getpalette():
                im.putpalette(p)

            new_frame = Image.new('RGBA', im.size)
            if mode == 'partial':
                new_frame.paste(last_frame)
            new_frame.paste(im, (0, 0), im.convert('RGBA'))
            new_frame.thumbnail(resize_to, Image.ANTIALIAS)

            all_frames.append(new_frame)
            i += 1
            last_frame = new_frame
            im.seek(im.tell() + 1)
    except EOFError:
        pass

    return all_frames

def compress_image(url:str, img_bytes:bytes, output_path:str) -> bool:
    try:
        image = Image.open(BytesIO(img_bytes))
        image_format = image.format
        if image_format in ["JPEG", "JPG", "WEBP", "PNG", "AVIF"]: # do NOT convert GIFs
            if image.width > IMAGE_WIDTH:
                ratio = IMAGE_WIDTH / float(image.width)
                new_height = int(image.height * ratio)
                image = image.resize((IMAGE_WIDTH, new_height), Image.LANCZOS)
            image = image.convert("RGB") # to ensure compatibility
            ImageFile.MAXBLOCK = image.size[0] * image.size[1]
            save_image_with_timeout(image, f"{os.path.splitext(output_path)[0]}.{IMAGE_FORMAT.lower()}")
            return True
        elif image_format in ["GIF"]:
            resize_gif(img_bytes, output_path)
            return True
    except Exception as e:
        print(f"Error compressing image {url}: {e}")
    return False

def save_image_handler(q:mp.Queue, image:Image, filename:str):
    try:
        image.save(filename, format=IMAGE_FORMAT, quality=IMAGE_QUALITY, optimize=True, progressive=True)
        q.put(None)
    except Exception as e:
        q.put(e)

def save_image_with_timeout(image:Image, filename:str, timeout=10):
    q = mp.Queue()
    p = mp.Process(target=save_image_handler, args=(q, image, filename))
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.terminate()
        raise RuntimeError("Save timed out")
    err = q.get()
    if err:
        raise err

def cache_image(url:str, post_id:str) -> str:
    print(f"  ⏱ Caching: {url}")
    r = http_request(url)
    ext = os.path.splitext(urlparse(url).path)[1].strip('.') or get_mime_type(r.headers).split("/")[1]
    img_name = f"{post_id}-{hashed_id(url)}.{ext}"
    local_path = os.path.join(MEDIA_DIR, img_name)
    if compress_image(url, r.content, local_path):
        img_name = f"{os.path.splitext(img_name)[0]}.{IMAGE_FORMAT.lower()}"
    else:
        with open(local_path, "wb") as f:
            f.write(r.content)
    print(f"  ✓ Cached: {img_name}")
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
            print(f"Failed to download {abs_url}: {e} [download_media_and_replace]")
            return None
    return [str(soup), first_img]

def fallback_cover_img(link:str, post_id:hashed_id):
    try:
        r = http_request(link)
        if get_mime_type(r.headers) == "text/html":
            cover_imgs = html_soup(r.content).select('meta[property="og:image"]')
            if len(cover_imgs):
                cover_url = cover_imgs[0].get("content") or None
                if cover_url:
                    try:
                        return cache_image(cover_url, post_id)
                    except Exception as e:
                        print(f"Failed to download {cover_url}: {e} [fallback_cover_img>try]")
    except Exception as e:
        print(f"Failed to download {link}: {e} [fallback_cover_img>except]")
    return None

def make_filename(title:str, date:datetime, link:str) -> str:
    slug = slugify(title or "untitled")[:50]
    return f"{date.strftime('%Y-%m-%d')}-{slug}-{hashed_id(link)}.html"

def validate_feeds(feeds):
    # if needed fields are missing, this will just immediately throw
    for feed in feeds:
        feed = feeds[feed]
        feed["url"], feed["category"]

def seen_urls_append(new_seen, link:str, guid:str):
    new_seen.append(link)
    if guid and guid != link:
        new_seen.append(guid)

def seen_urls_save(new_seen):
    with open(SEEN_FILE, "a") as f:
        for url in new_seen:
            f.write(f"{url}\n")

def handle_feed(feed, feeds, seen, new_seen):
    feed_id = feed
    feed = feeds[feed_id]
    feed_url = feed["url"]
    category = feed["category"]
    print(f"Fetching: {feed_url}")
    d = feedparser.parse(feed_url)

    for entry in d.entries:
        handle_entry(feed_id, entry, seen, new_seen)

def handle_entry(feed_id, entry, seen, new_seen):
    link = entry.get("link")
    guid = entry.get("id")
    if not link or (link in seen) or (guid and guid in seen):
        print(f"⏭ Seen: {link or guid}")
        return
    else:
        print(f"❤ New: {link or guid}")

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
        return

    if not cover_img:
        cover_img = fallback_cover_img(link, post_id)

    try:
        article_html = newspaper.article(link).article_html
        if article_html and article_html.strip():
            content_html = article_html
    except Exception as e:
        print(f"Failed to parse {link}: {e}")
        if type(e) != newspaper.ArticleBinaryDataException:
            return

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

    seen_urls_append(new_seen, link, guid)
    print(f"✓ Saved: {filename}")

def main():
    with open("_data/sources.yml", "r", encoding="utf-8") as f:
        feeds = yaml.safe_load(f)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)

    seen = set()
    new_seen = []

    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            seen = set(f.read().splitlines())

    validate_feeds(feeds)

    try:
        for feed in feeds:
            handle_feed(feed, feeds, seen, new_seen)
    except KeyboardInterrupt:
        print("Exiting!")
        exit()

    seen_urls_save(new_seen)
    print("Finished!")

if __name__ == "__main__":
    main()
