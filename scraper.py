#!/usr/bin/env python3
"""Short Tracker - Multi-Source Scraper"""

import requests, json, re, time, sys, os
from datetime import datetime, date, timedelta
from bs4 import BeautifulSoup

# ── Filters (env vars from GitHub Actions inputs) ─────────────────────────────
def _env(key, default):
    val = os.environ.get(key, "").strip()
    return val if val else default

FILTER_FLOAT_MAX  = _env("FILTER_FLOAT_MAX",  "5000000")
FILTER_PRICE_MIN  = _env("FILTER_PRICE_MIN",  "0.8")
FILTER_PRICE_MAX  = _env("FILTER_PRICE_MAX",  "6")
FILTER_AVAIL_MAX  = _env("FILTER_AVAIL_MAX",  "100000")
FILTER_BORROW_MIN = _env("FILTER_BORROW_MIN", "0")

print(f"Filters: float<{FILTER_FLOAT_MAX}  price${FILTER_PRICE_MIN}-${FILTER_PRICE_MAX}  avail<{FILTER_AVAIL_MAX}  borrow>{FILTER_BORROW_MIN}%")

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})

def safe_get(url, **kwargs):
    try:
        r = S.get(url, timeout=25, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  WARN {url}: {e}", file=sys.stderr)
        return None

TICKER_RE = re.compile(r'^[A-Z]{1,6}$')
def is_ticker(s):
    return bool(s and TICKER_RE.match(str(s).strip()))

# ── 1. RegSHO ─────────────────────────────────────────────────────────────────
def scrape_regsho():
    print("→ RegSHO...")
    tickers = set()
    date_str = None
    for delta in range(7):
        d = date.today() - timedelta(days=delta)
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y%m%d")
        found = False
        for prefix in ["nasdaqth", "nyseth", "arcath"]:
            url = f"https://www.nasdaqtrader.com/dynamic/symdir/regsho/{prefix}{ds}.txt"
            r = safe_get(url)
            if not r or r.status_code != 200:
                continue
            for line in r.text.strip().splitlines():
                t = line.split("|")[0].strip()
                if is_ticker(t):
                    tickers.add(t)
                    found = True
        if found:
            date_str = d.strftime("%b %-d, %Y")
            break
    print(f"   {len(tickers)} tickers ({date_str})")
    return sorted(tickers), date_str

# ── 2. ChartExchange Screener ─────────────────────────────────────────────────
# view_cols order (after optional leading # col):
# display, borrow_fee_rate_ib, borrow_fee_avail_ib, shares_float,
# market_cap, reg_price, reg_change_pct, reg_volume, 10_day_avg_vol,
# shortvol_all_short, shortint_db_pct, shortint_pct,
# shortint_position_change_pct, shortvol_all_short_pct,
# shortvol_all_short_pct_30d, pre_price, pre_change_pct
FIELD_ORDER = [
    "ticker", "borrow_rate", "avail_shares", "float",
    "market_cap", "price", "change_pct", "volume", "avg_vol_10d",
    "shortvol", "shortint_db_pct", "short_int_pct",
    "si_change_pct", "shortvol_pct", "shortvol_pct_30d",
    "pre_price", "pre_change_pct",
]

def scrape_chartexchange():
    print("→ ChartExchange Screener...")
    results = []
    page = 1

    # Build price filter: ChartExchange format  %3C = <  %3E = >
    price_filter = f"%3C{FILTER_PRICE_MAX},%3E{FILTER_PRICE_MIN}"
    # Borrow rate filter
    borrow_filter = f"%3E{FILTER_BORROW_MIN}" if float(FILTER_BORROW_MIN) > 0 else ""
    borrow_param  = f"&borrow_fee_rate_ib={borrow_filter}" if borrow_filter else ""

    base = (
        "https://chartexchange.com/screener/?page={page}"
        "&equity_type=ad,cs"
        "&exchange=BATS,NASDAQ,NYSE,NYSEAMERICAN"
        "&currency=USD"
        f"&shares_float=%3C{FILTER_FLOAT_MAX}"
        f"&reg_price={price_filter}"
        f"&borrow_fee_avail_ib=%3C{FILTER_AVAIL_MAX}"
        f"{borrow_param}"
        "&per_page=100"
        "&view_cols=display,borrow_fee_rate_ib,borrow_fee_avail_ib,shares_float,"
        "market_cap,reg_price,reg_change_pct,reg_volume,10_day_avg_vol,"
        "shortvol_all_short,shortint_db_pct,shortint_pct,"
        "shortint_position_change_pct,shortvol_all_short_pct,"
        "shortvol_all_short_pct_30d,pre_price,pre_change_pct"
        "&sort=borrow_fee_rate_ib,desc"
        "&section_saved=hide&section_select=hide&section_filter=hide&section_view=hide"
    )

    while True:
        r = safe_get(base.format(page=page))
        if not r:
            break

        soup = BeautifulSoup(r.text, "lxml")
        table = soup.find("table")
        if not table:
            print(f"  No table page {page}", file=sys.stderr)
            break

        # Detect if first column is a row-counter (#) by checking header text
        header_cells = table.find("tr").find_all(["th", "td"])
        header_texts = [c.get_text(strip=True) for c in header_cells]
        # If first header is "#" or a number, data starts at col 1
        offset = 1 if (header_texts and header_texts[0] in ("#", "", "No", "Row")) else 0
        # Also detect by finding which col has "Symbol" / "display" label
        for i, h in enumerate(header_texts):
            hl = h.lower().replace(" ", "")
            if hl in ("symbol", "display", "ticker"):
                offset = i
                break

        data_rows = table.find_all("tr")[1:]
        if not data_rows:
            break

        count = 0
        for row in data_rows:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells:
                continue

            # Slice from offset
            vals = cells[offset:]
            if not vals:
                continue

            # First value should be ticker
            ticker = vals[0].strip() if vals else ""
            if not is_ticker(ticker):
                # Try to find ticker anywhere in the row
                ticker = next((c for c in cells if is_ticker(c)), "")
            if not ticker:
                continue

            rec = {"ticker": ticker}
            for i, field in enumerate(FIELD_ORDER[1:], start=1):
                rec[field] = vals[i].strip() if i < len(vals) else "-"
                if not rec[field]:
                    rec[field] = "-"

            results.append(rec)
            count += 1

        print(f"   Page {page}: {count} rows")
        if count < 95:
            break
        page += 1
        time.sleep(1.0)

    print(f"   Total: {len(results)}")
    return results

# ── 3. AskEdgar Float ─────────────────────────────────────────────────────────
def scrape_float_askedgar(tickers):
    print(f"→ AskEdgar Float ({len(tickers)} tickers)...")
    floats = {}
    patterns = [
        re.compile(r'"(?:sharesFloat|shares_float|floatShares|shareFloat)"\s*:\s*"?([0-9][0-9,\.]+)"?', re.I),
        re.compile(r'(?:shares?\s*)?float\s*[:\-=]\s*([0-9][0-9,\.]+\s*[MBK]?)', re.I),
        re.compile(r'"float"\s*:\s*([0-9][0-9,\.]+)', re.I),
    ]

    for i, ticker in enumerate(tickers[:100]):
        r = safe_get(f"https://app.askedgar.io/{ticker}",
                     headers={**S.headers, "Referer": "https://app.askedgar.io/"})
        if not r:
            continue

        val = None
        ct = r.headers.get("content-type", "")

        if "json" in ct:
            try:
                d = r.json()
                for k in ("sharesFloat","shares_float","float","floatShares","shareFloat","Float"):
                    if d.get(k):
                        val = str(d[k])
                        break
                if not val:
                    for section in ("data","stats","fundamentals","summary"):
                        sec = d.get(section)
                        if isinstance(sec, dict):
                            for k in ("sharesFloat","shares_float","float","floatShares"):
                                if sec.get(k):
                                    val = str(sec[k])
                                    break
                        if val:
                            break
            except Exception:
                pass

        if not val:
            text = r.text
            # Check __NEXT_DATA__
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
            if m:
                try:
                    nd_str = m.group(1)
                    for pat in patterns:
                        fm = pat.search(nd_str)
                        if fm:
                            val = fm.group(1).strip()
                            break
                except Exception:
                    pass
            if not val:
                for pat in patterns:
                    fm = pat.search(text)
                    if fm:
                        candidate = fm.group(1).strip()
                        if re.match(r'^[\d,\.]+[MBK]?$', candidate):
                            val = candidate
                            break

        if val:
            floats[ticker] = val.replace(",", "")

        if (i+1) % 20 == 0:
            print(f"   {i+1}/{min(len(tickers),100)} ({len(floats)} found)")
        time.sleep(0.4)

    print(f"   Got float: {len(floats)}")
    return floats

# ── 4. Recent Splits — StockAnalysis ─────────────────────────────────────────
def scrape_splits_recent():
    print("→ StockAnalysis Recent Splits...")
    r = safe_get("https://stockanalysis.com/actions/splits/")
    if not r:
        return []

    # Try __NEXT_DATA__
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if m:
        try:
            nd = json.loads(m.group(1))
            rows = nd["props"]["pageProps"].get("data") or []
            out = []
            for row in rows:
                ratio = str(row.get("splitRatio") or row.get("ratio") or "")
                rtype = str(row.get("type") or "").lower()
                if "reverse" in rtype or re.search(r'1\s*[:/]\s*\d+', ratio):
                    out.append({
                        "ticker":  str(row.get("symbol") or ""),
                        "company": str(row.get("name") or ""),
                        "ratio":   ratio,
                        "date":    str(row.get("date") or ""),
                    })
            print(f"   {len(out)} recent reverse splits")
            return out
        except Exception:
            pass

    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table")
    if not table:
        return []

    headers = [th.get_text(strip=True).lower() for th in table.find("tr").find_all(["th","td"])]
    def ci(*names):
        for n in names:
            for i,h in enumerate(headers):
                if n in h: return i
        return None

    i_sym = ci("symbol","ticker"); i_co = ci("company","name")
    i_rat = ci("ratio","split");   i_dt = ci("date")
    i_typ = ci("type")

    out = []
    for row in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if not cells: continue
        gc = lambda idx: cells[idx] if idx is not None and idx < len(cells) else ""
        ratio = gc(i_rat); rtype = gc(i_typ).lower()
        if not ("reverse" in rtype or re.search(r'1\s*[:/]\s*\d+', ratio)):
            continue
        out.append({"ticker": gc(i_sym), "company": gc(i_co),
                    "ratio": ratio or "reverse", "date": gc(i_dt)})
    print(f"   {len(out)} recent reverse splits")
    return out

# ── 5. Upcoming Splits — TipRanks ────────────────────────────────────────────
def scrape_splits_upcoming():
    print("→ TipRanks Upcoming Splits...")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
            page = browser.new_context(user_agent=S.headers["User-Agent"]).new_page()
            page.goto("https://www.tipranks.com/calendars/stock-splits/upcoming",
                      wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(5000)
            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        if not table:
            return []

        headers = [th.get_text(strip=True).lower() for th in table.find("tr").find_all(["th","td"])]
        print(f"   TipRanks headers: {headers}")

        def ci(*names):
            for n in names:
                for i,h in enumerate(headers):
                    if n in h: return i
            return None

        i_sym = ci("ticker","symbol"); i_co = ci("company","name")
        i_rat = ci("ratio","split");   i_dt = ci("date","ex-date")
        i_typ = ci("type")

        out = []
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells: continue
            gc = lambda idx: cells[idx] if idx is not None and idx < len(cells) else ""
            rtype = gc(i_typ).lower(); ratio = gc(i_rat)
            # If we can determine type, skip forward splits
            if i_typ is not None and rtype and "reverse" not in rtype:
                continue
            out.append({"ticker": gc(i_sym), "company": gc(i_co),
                        "ratio": ratio or rtype or "reverse", "date": gc(i_dt)})
        print(f"   {len(out)} upcoming")
        return out
    except Exception as e:
        print(f"  TipRanks error: {e}", file=sys.stderr)
        return []

# ── 6. Ticker Changes ─────────────────────────────────────────────────────────
def scrape_changes_nasdaq():
    print("→ NASDAQ Ticker Changes...")
    r = safe_get(
        "https://api.nasdaq.com/api/quote/list-type/symbolchangehistory?offset=0&limit=100",
        headers={**S.headers, "Accept": "application/json", "Origin": "https://www.nasdaq.com"}
    )
    if r:
        try:
            d = r.json()
            rows = ((d.get("data") or {}).get("data") or d.get("rows") or [])
            if rows:
                out = [{"new_ticker": str(row.get("newSymbol","")),"old_ticker": str(row.get("oldSymbol","")),
                        "date": str(row.get("dateOfChange","")),"source":"NASDAQ"} for row in rows]
                print(f"   {len(out)} NASDAQ (API)"); return out
        except Exception: pass

    r = safe_get("https://www.nasdaq.com/market-activity/stocks/symbol-change-history?page=1&rows_per_page=100")
    if not r: return []
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table")
    out = []
    if table:
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) >= 2:
                out.append({"new_ticker":cells[0],"old_ticker":cells[1],
                            "date":cells[2] if len(cells)>2 else "","source":"NASDAQ"})
    print(f"   {len(out)} NASDAQ"); return out

def scrape_changes_stockanalysis():
    print("→ StockAnalysis Ticker Changes...")
    r = safe_get("https://stockanalysis.com/actions/changes/")
    if not r: return []

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if m:
        try:
            nd = json.loads(m.group(1))
            rows = nd["props"]["pageProps"].get("data") or []
            out = [{"new_ticker": str(row.get("newSymbol") or row.get("symbol","")),"old_ticker": str(row.get("oldSymbol","")),"date": str(row.get("date","")),"source":"StockAnalysis"} for row in rows]
            print(f"   {len(out)} SA"); return out
        except Exception: pass

    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table")
    if not table: return []
    headers = [th.get_text(strip=True).lower() for th in table.find("tr").find_all(["th","td"])]
    def ci(*names):
        for n in names:
            for i,h in enumerate(headers):
                if n in h: return i
        return None
    i_new = ci("new","symbol") or 0; i_old = ci("old","prev") or 1; i_dt = ci("date") or 2
    out = []
    for row in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells)>=2:
            out.append({"new_ticker":cells[i_new] if i_new<len(cells) else "","old_ticker":cells[i_old] if i_old<len(cells) else "","date":cells[i_dt] if i_dt<len(cells) else "","source":"StockAnalysis"})
    print(f"   {len(out)} SA"); return out

# ── 7. DilutionTracker S1 ─────────────────────────────────────────────────────
def scrape_s1():
    print("→ DilutionTracker S1 (Playwright)...")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=S.headers["User-Agent"])
            page = ctx.new_page()

            api_data = []
            def on_resp(resp):
                if any(x in resp.url for x in ["api","s1","filing","pending"]):
                    try: api_data.append(resp.json())
                    except: pass
            page.on("response", on_resp)

            page.goto("https://dilutiontracker.com/app/s1",
                      wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(7000)
            html = page.content()
            browser.close()

        # Try intercepted API
        for d in api_data:
            rows = d.get("data") or d.get("filings") or d.get("pending") or (d if isinstance(d, list) else [])
            if rows and isinstance(rows, list) and len(rows) > 0:
                out = []
                for row in rows[:500]:
                    if not isinstance(row, dict): continue
                    out.append({
                        "ticker":            str(row.get("ticker") or row.get("symbol") or ""),
                        "date_s1":           str(row.get("dateOfFirstS1") or row.get("date") or row.get("filedDate") or ""),
                        "pricing_date":      str(row.get("pricingDate") or row.get("pricing_date") or ""),
                        "deal_size":         str(row.get("anticipatedDealSize") or row.get("dealSize") or row.get("deal_size") or ""),
                        "warrant_coverage":  str(row.get("estimatedWarrantCoverage") or row.get("warrantCoverage") or row.get("warrant_coverage") or ""),
                        "underwriter":       str(row.get("underwriters") or row.get("placementAgents") or row.get("underwriter") or ""),
                        "float_before":      str(row.get("floatBeforeOffering") or row.get("float") or row.get("sharesFloat") or ""),
                        "shares_offered":    str(row.get("sharesOffered") or ""),
                        "exercise_price":    str(row.get("exercisePrice") or row.get("warrantExercisePrice") or ""),
                    })
                if out:
                    print(f"   {len(out)} S1 (API)")
                    return out

        # HTML fallback — read all columns dynamically
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        if not table:
            print("  DilutionTracker: no table", file=sys.stderr)
            return []

        headers = [th.get_text(strip=True) for th in table.find("tr").find_all(["th","td"])]
        print(f"   DT headers: {headers}")

        def ci(*names):
            for n in names:
                for i,h in enumerate(headers):
                    if n.lower() in h.lower(): return i
            return None

        i_tick   = ci("Ticker","Symbol")
        i_date   = ci("Date of first","Filed","Date")
        i_price  = ci("Pricing Date","Pricing")
        i_deal   = ci("Deal Size","Anticipated")
        i_warr   = ci("Warrant Coverage","Estimated")
        i_under  = ci("Underwriter","Placement","Agent")
        i_float  = ci("Float","Shares Float")
        i_soff   = ci("Shares offered","Shares Offered")
        i_exer   = ci("Exercise price","Exercise Price")

        out = []
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells: continue
            gc = lambda idx: cells[idx].strip() if idx is not None and idx < len(cells) else ""
            ticker = gc(i_tick) if i_tick is not None else (cells[0] if cells else "")
            if not ticker: continue
            out.append({
                "ticker":           ticker,
                "date_s1":          gc(i_date),
                "pricing_date":     gc(i_price),
                "deal_size":        gc(i_deal),
                "warrant_coverage": gc(i_warr),
                "underwriter":      gc(i_under),
                "float_before":     gc(i_float),
                "shares_offered":   gc(i_soff),
                "exercise_price":   gc(i_exer),
            })
        print(f"   {len(out)} S1 (HTML)")
        return out

    except ImportError:
        print("  Playwright not installed", file=sys.stderr); return []
    except Exception as e:
        print(f"  DilutionTracker error: {e}", file=sys.stderr); return []

# ── 8. Finviz Insider Buys ────────────────────────────────────────────────────
def scrape_insiders():
    print("→ Finviz Insider Buys...")
    for url in ["https://finviz.com/insidertrading.ashx?tc=1",
                "https://finviz.com/insidertrading?tc=1"]:
        r = safe_get(url, headers={**S.headers, "Referer": "https://finviz.com/"})
        if r: break
    if not r: return []

    soup = BeautifulSoup(r.text, "lxml")
    table = None
    for t in soup.find_all("table"):
        if any(x in t.get_text() for x in ["Purchase","Sale","Buy"]):
            table = t; break
    if not table: return []

    rows = table.find_all("tr")
    headers = []; hi = 0
    for i, row in enumerate(rows):
        cells = [c.get_text(strip=True) for c in row.find_all(["td","th"])]
        if "Ticker" in cells or "Owner" in cells:
            headers = [c.lower() for c in cells]; hi = i; break

    def ci(*names):
        for n in names:
            for i,h in enumerate(headers):
                if n in h: return i
        return None

    i_tick = ci("ticker"); i_own = ci("owner","insider"); i_rel = ci("relationship","title")
    i_dt = ci("date"); i_tr = ci("transaction","type"); i_cost = ci("cost","price")
    i_sh = ci("#shares","shares"); i_val = ci("value")

    out = []
    for row in rows[hi+1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) < 4: continue
        gc = lambda idx: cells[idx] if idx is not None and idx < len(cells) else ""
        ticker = gc(i_tick) if i_tick is not None else (cells[1] if len(cells)>1 else "")
        if not is_ticker(ticker): continue
        out.append({"ticker":ticker,"owner":gc(i_own),"relationship":gc(i_rel),
                    "date":gc(i_dt),"transaction":gc(i_tr),"cost":gc(i_cost),
                    "shares":gc(i_sh),"value":gc(i_val)})
    print(f"   {len(out)} insider rows")
    return out

# ── MAIN ──────────────────────────────────────────────────────────────────────
def build():
    result = {
        "updated":"—","regsho_tickers":[],"regsho_date":"",
        "filters":{"float_max":FILTER_FLOAT_MAX,"price_min":FILTER_PRICE_MIN,"price_max":FILTER_PRICE_MAX,"avail_max":FILTER_AVAIL_MAX,"borrow_min":FILTER_BORROW_MIN},
        "screener":[],"splits_recent":[],"splits_upcoming":[],
        "ticker_changes":[],"s1_filings":[],"insiders":[],
    }

    regsho_list, regsho_date = scrape_regsho()
    result["regsho_tickers"] = regsho_list
    result["regsho_date"]    = regsho_date or ""
    regsho_set = set(regsho_list)

    screener = scrape_chartexchange()
    for row in screener:
        row["reg_sho"] = row["ticker"] in regsho_set

    # AskEdgar float for all screener tickers
    all_tickers = [r["ticker"] for r in screener]
    fmap = scrape_float_askedgar(all_tickers)
    for row in screener:
        if row["ticker"] in fmap:
            row["float"] = fmap[row["ticker"]]

    s1 = scrape_s1()
    result["s1_filings"] = s1
    s1_map = {r["ticker"]: r for r in s1}

    insiders = scrape_insiders()
    result["insiders"] = insiders
    buyer_map = {ins["ticker"]: ins["owner"] for ins in reversed(insiders)}

    for row in screener:
        s1r = s1_map.get(row["ticker"])
        row["s1_date"] = s1r["date_s1"] if s1r else "-"
        row["buyer"]   = buyer_map.get(row["ticker"], "-")

    result["screener"] = screener
    result["updated"]  = datetime.now().strftime("%b %-d, %Y %H:%M UTC")

    result["splits_recent"]   = scrape_splits_recent()
    result["splits_upcoming"] = scrape_splits_upcoming()

    seen, merged = set(), []
    for row in scrape_changes_nasdaq() + scrape_changes_stockanalysis():
        key = (row["new_ticker"], row["old_ticker"])
        if key not in seen:
            seen.add(key); merged.append(row)
    result["ticker_changes"] = merged

    return result

if __name__ == "__main__":
    print("="*60 + "\nShort Tracker Scraper\n" + "="*60)
    data = build()
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n✅ data.json written")
    print(f"   Screener:    {len(data['screener'])}")
    print(f"   RegSHO:      {len(data['regsho_tickers'])}")
    print(f"   Splits R/U:  {len(data['splits_recent'])}/{len(data['splits_upcoming'])}")
    print(f"   Changes:     {len(data['ticker_changes'])}")
    print(f"   S1:          {len(data['s1_filings'])}")
    print(f"   Insiders:    {len(data['insiders'])}")
