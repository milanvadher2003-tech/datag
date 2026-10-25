# zomato_scraper.py — GitHub Actions ready
import os, json, time, sys, re
import functools
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests
import pandas as pd

# 🔥 यह लाइन जोड़ी है ताकि GitHub पर लॉग्स लाइव दिखें (Live Logs)
print = functools.partial(print, flush=True)

# ============================================================
# cURL — GitHub Secret "ZOMATO_CURL" से आएगा
# लोकल टेस्ट के लिए env var सेट करें या नीचे paste करें
# ============================================================
CURL_STRING = os.environ.get("ZOMATO_CURL", "").strip()

if not CURL_STRING:
    print("❌ ZOMATO_CURL env/secret नहीं मिला।")
    print("   GitHub → Settings → Secrets → Actions → New secret:")
    print("   Name: ZOMATO_CURL")
    print("   Value: 'Copy as cURL (bash)' वाला पूरा command (caret ^ हटाकर)")
    sys.exit(1)


# ============================================================
# cURL PARSER (Windows ^ और bash \ दोनों handle करता है)
# ============================================================
def _clean_curl(curl_text):
    t = curl_text
    # Windows CMD escapes हटाओ
    t = t.replace('^"', '"').replace('^^', '^^PH^^')
    t = t.replace('^', '')
    t = t.replace('^^PH^^', '^')
    # bash line-continuation हटाओ
    t = t.replace('\\\n', ' ').replace('\\\r\n', ' ')
    t = t.replace('\\"', '"')
    # multiple whitespace → single space (headers के अंदर नहीं, बाहर)
    return t


def parse_curl(curl_text):
    text = _clean_curl(curl_text)
    cookies, headers = {}, {}

    # URL निकालो
    url_match = re.search(r'--url\s+["\']?([^"\'\s]+)', text) or \
                re.search(r"curl\s+['\"]?(https?://[^'\"\s]+)", text)
    url = url_match.group(1) if url_match else ""

    res_id_match = re.search(r'[?&]res_id=(\d+)', url)
    res_id = res_id_match.group(1) if res_id_match else None

    # Headers निकालो (-H 'key: value')
    for m in re.finditer(r"-H\s+['\"]([^'\"]+)['\"]", text):
        h = m.group(1)
        if ':' in h:
            k, v = h.split(':', 1)
            headers[k.strip()] = v.strip()

    # Cookies निकालो (-b 'a=1; b=2' या --cookie)
    cookie_match = re.search(r'(?:-b|--cookie)\s+(["\'])(.+?)\1', text, re.I | re.S)
    if not cookie_match:
        cookie_match = re.search(r"(?:-H|--header)\s+['\"]cookie:\s*([^'\"]+)['\"]",
                                 text, re.I | re.S)
    if cookie_match:
        cookie_str = cookie_match.group(2) if cookie_match.lastindex and cookie_match.lastindex >= 2 \
                     else cookie_match.group(1)
        for part in cookie_str.split(';'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                cookies[k.strip()] = v.strip()

    csrf = headers.get("x-zomato-csrft") or cookies.get("csrf") or ""

    return {
        "url": url,
        "res_id": res_id,
        "cookies": cookies,
        "csrf": csrf,
        "headers": headers,
    }


PARSED       = parse_curl(CURL_STRING)
MY_COOKIES   = PARSED["cookies"]
MY_CSRF      = PARSED["csrf"]
CURL_HEADERS = PARSED["headers"]
START_RES_ID = PARSED["res_id"]

# ============================================================
# CONFIG
# ============================================================
USER_ID, CITY_ID, CITY_NAME = "176840241", 11, "Ahmedabad"
PLACE_ID, CELL_ID = "3720", "4133887237286789120"
LAT, LON, SUBTONE = "23.042662", "72.566729", 3720

OUT_DIR = Path("zomato_data")
OUT_DIR.mkdir(exist_ok=True)
BASE = "https://www.zomato.com"

# env से override हो सकते हैं
MIN_DELAY        = float(os.environ.get("MIN_DELAY", "0.8"))
MAX_WORKERS      = int(os.environ.get("MAX_WORKERS", "6"))
MAX_REVIEW_PAGES = int(os.environ.get("MAX_REVIEW_PAGES", "10"))
MAX_SEARCH_PAGES = int(os.environ.get("MAX_PAGES", "20"))

# ============================================================
# HEADERS
# ============================================================
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/155.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8,gu;q=0.7",
    "Content-Type": "application/json",
    "x-zomato-csrft": MY_CSRF,
    "Referer": f"{BASE}/{CITY_NAME.lower()}/restaurants",
    "Origin": BASE,
}
for k, v in CURL_HEADERS.items():
    if k.lower() not in ("content-length", "host", "cookie"):
        HEADERS[k] = v
if MY_CSRF:
    HEADERS["x-zomato-csrft"] = MY_CSRF

# ============================================================
# SAFE HTTP
# ============================================================
_UNAUTH_COUNT = 0

def _call(url, method="GET", body=None, retries=4):
    global _UNAUTH_COUNT
    for attempt in range(retries):
        try:
            time.sleep(MIN_DELAY)
            if method == "POST":
                r = requests.post(url, headers=HEADERS, cookies=MY_COOKIES,
                                  json=body, impersonate="chrome124", timeout=30)
            else:
                r = requests.get(url, headers=HEADERS, cookies=MY_COOKIES,
                                 impersonate="chrome124", timeout=30)

            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"   429, {wait}s रुके..."); time.sleep(wait); continue

            if r.status_code == 401:
                _UNAUTH_COUNT += 1
                wait = 5 * (attempt + 1)
                print(f"   ⚠️ 401 (कुल {_UNAUTH_COUNT}) — {wait}s रुककर retry...")
                time.sleep(wait); continue

            if r.status_code == 403:
                print(f"   ❌ 403: {r.text[:120]}")
                return None

            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"   attempt {attempt+1}: {str(e)[:80]}")
            time.sleep(3 * (attempt + 1))
    return None

# ============================================================
# EXTRACTORS
# ============================================================
def _safe_dict(x): return x if isinstance(x, dict) else {}
def _safe_list(x): return x if isinstance(x, list) else []

def extract_basic(res_json):
    if not isinstance(res_json, dict): return {}
    page = _safe_dict(res_json.get("page_data"))
    sec  = _safe_dict(page.get("sections"))
    basic   = _safe_dict(sec.get("SECTION_BASIC_INFO"))
    contact = _safe_dict(sec.get("SECTION_RES_CONTACT"))
    details = _safe_dict(sec.get("SECTION_RES_DETAILS"))

    cft_list = _safe_list(_safe_dict(details.get("CFT_DETAILS")).get("cfts"))
    cft_text = ""
    if cft_list and isinstance(cft_list[0], dict):
        cft_text = cft_list[0].get("title", "") or ""

    hl_raw = _safe_list(_safe_dict(details.get("HIGHLIGHTS")).get("highlights"))
    highlights = [h.get("text", "") for h in hl_raw if isinstance(h, dict)]
    timing = _safe_dict(basic.get("timing")).get("timing_desc", "") or ""

    return {
        "resId":        basic.get("res_id"),
        "name":         basic.get("name"),
        "cuisines":     basic.get("cuisine_string"),
        "rating":       _safe_dict(basic.get("rating")).get("aggregate_rating"),
        "votes":        _safe_dict(basic.get("rating")).get("votes"),
        "cost_for_two": cft_text or (_safe_dict(basic.get("cft")).get("text") or ""),
        "address":      contact.get("address"),
        "locality":     contact.get("locality_verbose"),
        "city":         contact.get("city_name"),
        "phone":        _safe_dict(contact.get("phoneDetails")).get("phoneStr"),
        "highlights":   " | ".join(highlights),
        "resUrl":       basic.get("resUrl"),
        "timing":       timing,
    }

def extract_reviews(res_id, reviews_json):
    rows = []
    if not isinstance(reviews_json, dict): return rows
    ents = _safe_dict(_safe_dict(reviews_json.get("entities")).get("REVIEWS"))
    for rid, r in ents.items():
        if not isinstance(r, dict): continue
        txt = (r.get("reviewText") or "")
        txt = re.sub(r"<br\s*/?>", " ", txt).strip()
        rows.append({
            "resId":      res_id,
            "reviewId":   rid,
            "user":       r.get("userName"),
            "rating":     r.get("ratingV2"),
            "timestamp":  r.get("timestamp"),
            "experience": r.get("experience"),
            "likes":      r.get("likeCount", 0),
            "text":       txt,
        })
    return rows

def extract_menu(res_id, menu_json):
    rows = []
    if not isinstance(menu_json, dict): return rows
    page = _safe_dict(menu_json.get("page_data"))
    sec  = _safe_dict(page.get("sections"))
    details = _safe_dict(sec.get("SECTION_RES_DETAILS"))

    for m in _safe_list(_safe_dict(details.get("IMAGE_MENUS")).get("menus")):
        if not isinstance(m, dict): continue
        rows.append({
            "resId": res_id, "menu_type": "image",
            "menu_label": m.get("label"), "category": "",
            "item_name": "", "price": "",
            "desc": m.get("subtitle"), "thumb": m.get("thumb"),
        })

    order = _safe_dict(page.get("order"))
    menu_list = _safe_list(_safe_dict(order.get("menuList")).get("menus"))
    for m in menu_list:
        if not isinstance(m, dict): continue
        menu = _safe_dict(m.get("menu"))
        for cat in _safe_list(menu.get("categories")):
            if not isinstance(cat, dict): continue
            cat_obj = _safe_dict(cat.get("category"))
            for item in _safe_list(cat_obj.get("items")):
                if not isinstance(item, dict): continue
                it = _safe_dict(item.get("item"))
                rows.append({
                    "resId": res_id, "menu_type": "text",
                    "menu_label": menu.get("name"),
                    "category": cat_obj.get("name"),
                    "item_name": it.get("name"),
                    "price": it.get("price"),
                    "desc": it.get("desc"),
                    "thumb": it.get("item_image_url"),
                })
    return rows

def extract_offers(res_id, overview_json):
    rows = []
    if not isinstance(overview_json, dict): return rows
    page = _safe_dict(overview_json.get("page_data"))
    sec  = _safe_dict(page.get("sections"))
    offers_raw = sec.get("SECTION_DINING_OFFERS")
    if not isinstance(offers_raw, list): return rows
    for o in offers_raw:
        if not isinstance(o, dict): continue
        title    = re.sub(r"\s+", " ", str(o.get("title", "") or "")).strip()
        subtitle = re.sub(r"\s+", " ", str(o.get("subtitle", "") or "")).strip()
        rows.append({
            "resId": res_id, "title": title, "subtitle": subtitle,
            "offer_value": o.get("offer_value"),
            "offer_type": o.get("offerType"),
            "start": o.get("start_time"),
            "end": o.get("end_time"),
        })
    return rows

# ============================================================
# SEARCH
# ============================================================
def fetch_dining_search(max_pages=20):
    url = f"{BASE}/webroutes/search/home"
    all_rests, search_id, page, offset, total_shown = [], None, 1, 0, 0
    while page <= max_pages:
        if search_id:
            prev = json.dumps({"PreviousSearchId": search_id,
                "PreviousSearchFilter": [
                    json.dumps({"category_context": "go_out_home"}), "",
                    json.dumps({"context": "dineout_home"})]})
            post = json.dumps({"search_id": search_id, "solr_offset": offset,
                "page": page, "total_restaurants_shown": total_shown,
                "total_results_shown": total_shown})
            af = [
                {"filterType": "category_sheet", "filterValue": "go_out_home",
                 "isHidden": True, "isApplied": True,
                 "postKey": json.dumps({"category_context": "go_out_home"})},
                {"filterType": "context", "filterValue": "dineout_home",
                 "isHidden": True, "isApplied": True,
                 "postKey": json.dumps({"context": "dineout_home"})}]
        else:
            prev, post, af = "", "", []
        filters = {"searchMetadata": {
            "previousSearchParams": prev, "postbackParams": post,
            "totalResults": 0, "hasMore": True, "getInactive": False},
            "dineoutAdsMetaData": {}, "appliedFilter": af, "urlParamsForAds": {}}
        payload = {
            "context": "dineout", "filters": json.dumps(filters),
            "addressId": 0, "entityId": CITY_ID, "entityType": "city",
            "locationType": "", "isOrderLocation": 1, "cityId": CITY_ID,
            "latitude": LAT, "longitude": LON,
            "userDefinedLatitude": float(LAT), "userDefinedLongitude": float(LON),
            "entityName": CITY_NAME, "orderLocationName": CITY_NAME,
            "cityName": CITY_NAME, "countryId": 1, "countryName": "India",
            "displayTitle": CITY_NAME, "o2Serviceable": True,
            "placeId": PLACE_ID, "cellId": CELL_ID,
            "deliverySubzoneId": SUBTONE, "placeType": "DSZ",
            "placeName": CITY_NAME, "isO2City": True,
            "fetchFromGoogle": False, "fetchedFromCookie": True,
            "isO2OnlyCity": False, "address_template": [],
            "otherRestaurantsUrl": "",
        }
        print(f"[search] page {page} (offset {offset})...")
        data = _call(url, "POST", payload)
        if not data: break
        sections = _safe_dict(data.get("sections"))
        results  = _safe_list(sections.get("SECTION_SEARCH_RESULT"))
        meta     = _safe_dict(_safe_dict(sections.get("SECTION_SEARCH_META_INFO")).get("searchMetaData"))

        for item in results:
            if not isinstance(item, dict) or item.get("type") != "restaurant": continue
            info = _safe_dict(item.get("info"))
            all_rests.append({
                "resId":        info.get("resId"),
                "name":         info.get("name"),
                "rating":       _safe_dict(info.get("rating")).get("aggregate_rating"),
                "votes":        _safe_dict(info.get("rating")).get("votes"),
                "cost_for_two": (_safe_dict(info.get("cft")).get("text") or ""),
                "locality":     _safe_dict(info.get("locality")).get("name"),
                "address":      _safe_dict(info.get("locality")).get("address"),
                "cuisines":     ", ".join(c.get("name","") for c in _safe_list(info.get("cuisine")) if isinstance(c, dict)),
                "distance":     item.get("distance"),
                "is_promoted":  item.get("isPromoted"),
                "clickUrl":     _safe_dict(item.get("cardAction")).get("clickUrl"),
            })

        if not meta.get("hasMore"): break
        try:
            pp = json.loads(meta.get("postbackParams", "{}"))
            search_id   = pp.get("search_id", search_id)
            offset      = pp.get("solr_offset", offset + len(results))
            total_shown = pp.get("total_results_shown", total_shown + len(results))
        except Exception:
            offset += len(results); total_shown += len(results)
        page += 1
    return all_rests

# ============================================================
# ONE RESTAURANT
# ============================================================
def process_one_restaurant(r):
    rid, res_url = r["resId"], r["clickUrl"]
    out = {"overview": {}, "reviews": [], "menu": [], "offers": []}
    if not rid or not res_url:
        return rid, out

    ov = _call(f"{BASE}/webroutes/getPage?page_url={res_url}&location=&isMobile=0")
    if ov:
        out["overview"] = extract_basic(ov)
        out["offers"]   = extract_offers(rid, ov)

    for p in range(1, MAX_REVIEW_PAGES + 1):
        rv = _call(f"{BASE}/webroutes/reviews/loadMore?"
                   f"sort=dd&filter=reviews-dining&res_id={rid}&page={p}")
        if not rv: break
        rows = extract_reviews(rid, rv)
        if not rows: break
        out["reviews"].extend(rows)
        n = _safe_dict(_safe_dict(rv.get("page_data")).get("sections")) \
            .get("SECTION_REVIEWS", {}).get("numberOfPages", 1)
        if p >= n: break

    mn = _call(f"{BASE}/webroutes/getPage?page_url={res_url}/menu&location=&isMobile=0")
    out["menu"] = extract_menu(rid, mn)

    return rid, out

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    if not MY_COOKIES:
        print("❌ cURL से कोई कुकी नहीं मिली। ZOMATO_CURL secret ठीक से डालें।")
        sys.exit(1)
    print(f"✅ cURL से {len(MY_COOKIES)} कुकीज़ लोड हुईं")
    print(f"   CSRF: {MY_CSRF or '(नहीं मिला)'}")
    print(f"   zat:  {MY_COOKIES.get('zat', '(नहीं मिली)')[:30]}...")
    if START_RES_ID:
        print(f"   res_id (cURL से): {START_RES_ID}")
    print("=" * 60)
    print(f"Zomato → Excel | {USER_ID} | {CITY_NAME}")
    print(f"⚡ Speed: delay={MIN_DELAY}s, workers={MAX_WORKERS}, "
          f"search_pages={MAX_SEARCH_PAGES}, review_pages={MAX_REVIEW_PAGES}")
    print("=" * 60)

    t0 = time.time()

    rests = fetch_dining_search(max_pages=MAX_SEARCH_PAGES)
    if not rests:
        print("❌ search fail")
        if _UNAUTH_COUNT > 0:
            print(f"\n⚠️ {_UNAUTH_COUNT} बार 401 आया। संभावित कारण:")
            print("   1. search वाला cURL नहीं, api.zomato.com वाला डाला है।")
            print("   2. कुकीज़ expire हो गई हैं → ब्राउज़र में दोबारा लॉगिन करें।")
        sys.exit(1)
    print(f"✅ {len(rests)} restaurants मिले\n")

    all_overviews, all_reviews, all_menus, all_offers = [], [], [], []
    done = 0
    total = len(rests)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_one_restaurant, r): r for r in rests}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                rid, out = fut.result()
                if out["overview"]:
                    all_overviews.append(out["overview"])
                all_reviews.extend(out["reviews"])
                all_menus.extend(out["menu"])
                all_offers.extend(out["offers"])
            except Exception as e:
                print(f"  ⚠️ skip {r.get('name')}: {str(e)[:80]}")
            done += 1
            print(f"[{done}/{total}] ✅ {r['name']}  "
                  f"(reviews={len(all_reviews)}, menus={len(all_menus)})")

    print("\n📝 Excel बना रहे हैं...")
    xlsx_path = OUT_DIR / "zomato_data.xlsx"

    def _df(rows):
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as w:
        _df(rests).to_excel(w, sheet_name="Restaurants", index=False)
        _df(all_overviews).to_excel(w, sheet_name="Overview", index=False)
        _df(all_reviews).to_excel(w, sheet_name="Reviews", index=False)
        _df(all_menus).to_excel(w, sheet_name="Menu", index=False)
        _df(all_offers).to_excel(w, sheet_name="Offers", index=False)

    elapsed = time.time() - t0
    print(f"\n✅ Saved: {xlsx_path}")
    print(f"   Restaurants : {len(rests)}")
    print(f"   Overview    : {len(all_overviews)}")
    print(f"   Reviews     : {len(all_reviews)}")
    print(f"   Menu rows   : {len(all_menus)}")
    print(f"   Offers      : {len(all_offers)}")
    print(f"   ⏱  Total time: {elapsed:.1f}s")
