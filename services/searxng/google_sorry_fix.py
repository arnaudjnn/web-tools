"""Build-time patcher for SearXNG's Google engine.

Patch 1: Detect HTTP 302 sorry redirects in response()
Patch 2: Enrich LinkedIn results with location from surrounding HTML text.
         Google GSA truncates descriptions but the full HTML contains
         "City, State" patterns near LinkedIn profile links.
Patch 3: Fallback parser for degraded Google HTML format.
         Google sometimes serves a simplified HTML format (UBFage class links)
         that the default XPath selectors miss. This adds a fallback that
         extracts results from the degraded format.

Every patch must either apply or be verifiably upstream already; otherwise the
script exits 1 and the image build fails. A patch that silently stops matching
after an image bump is a search regression nobody sees until results degrade.

Usage: python3 google_sorry_fix.py /path/to/google.py
"""
import sys

google_py = sys.argv[1]

with open(google_py, "r") as f:
    code = f.read()

failed = []

# Patch 1: detect 302 sorry redirects
old1 = "    detect_google_sorry(resp)\n    data_image_map"
new1 = """    detect_google_sorry(resp)
    # Patch: detect 302 sorry redirects that bypass detect_google_sorry
    if resp.status_code == 302 or (len(resp.text) < 2000 and '/sorry/' in resp.text):
        raise SearxEngineCaptchaException(suspended_time=0)
    data_image_map"""

if old1 in code:
    code = code.replace(old1, new1)
    print("PATCH 1: 302/sorry detection added")
elif "status_code == 302" in code and '"/sorry/" in resp.text' in code:
    # Upstream detect_google_sorry() covers both cases since 2026.9 (verified on
    # searxng/searxng:2026.9.30-a9d990033).
    print("PATCH 1: already upstream (detect_google_sorry handles 302 + /sorry/)")
else:
    failed.append("1 (302/sorry detection)")

# Patch 2: enrich LinkedIn results with location from HTML context
old2 = '    return results'
new2 = r"""    # Patch 2: enrich LinkedIn results with location from nearby HTML text
    # Google GSA truncates descriptions but the full HTML has "City, State" near LinkedIn URLs
    import re as _re
    raw = resp.text
    for r in results:
        url = r.get('url', '') if isinstance(r, dict) else getattr(r, 'url', '')
        content = r.get('content', '') if isinstance(r, dict) else getattr(r, 'content', '')
        if 'linkedin.com/in/' not in str(url):
            continue
        if 'Location' in str(content):
            continue  # already has location

        # Find the LinkedIn URL in raw HTML and extract nearby City, State patterns
        handle_match = _re.search(r'linkedin\.com/in/([\w-]+)', str(url))
        if not handle_match:
            continue
        handle = handle_match.group(1)
        pos = raw.find(handle)
        if pos < 0:
            continue

        # Get text CLOSE to the LinkedIn result (strip HTML tags) — narrow window to avoid noise
        chunk = raw[max(0, pos - 500):pos + 2000]
        clean = _re.sub(r'<[^>]+>', ' ', chunk)
        clean = _re.sub(r'\s+', ' ', clean)

        # Look for explicit "Location: City" or "City, State, Country" with 3 parts
        city_match = _re.search(r'Location[:\s]+([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2})', clean)
        if not city_match:
            # Require 3-part "City, State, Country" for non-labeled locations
            city_match = _re.search(
                r'([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2}),\s*'
                r'([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2}),\s*'
                r'([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2})',
                clean
            )
        if city_match:
            location = city_match.group(1).strip()
            skip_words = {'Experience', 'Education', 'Founder', 'CEO', 'CTO', 'COO', 'Author',
                          'Investor', 'Engineer', 'Present', 'January', 'February', 'March',
                          'April', 'May', 'June', 'July', 'August', 'September', 'October',
                          'November', 'December', 'Graphic', 'Cofounder', 'Advisory'}
            if location not in skip_words and len(location) > 2 and len(location) < 25:
                new_content = str(content) + ' · Location: ' + city_match.group(0).strip()
                if isinstance(r, dict):
                    r['content'] = new_content
                elif hasattr(r, 'content'):
                    r.content = new_content

    # Patch 3: fallback parser for degraded Google HTML (UBFage format)
    # When Google serves a simplified HTML (different IP/TLS), the default XPath
    # selectors return 0 results. This fallback extracts from the UBFage format.
    organic_count = sum(1 for r in results if isinstance(r, dict) and 'url' in r and 'suggestion' not in r)
    if organic_count == 0:
        import logging as _logging
        _log = _logging.getLogger('searx.engines.google')
        _log.info("Patch 3: default selectors returned 0 results, trying fallback parser")
        from lxml import html as _html
        from urllib.parse import unquote as _unquote
        _dom = _html.fromstring(resp.text)

        for _a in _dom.xpath('//a[contains(@class, "UBFage")]'):
            try:
                _raw_url = _a.get('href', '')
                if not _raw_url or not _raw_url.startswith('/url?q='):
                    continue
                _url = _unquote(_raw_url[7:].split('&sa=U')[0])

                # Title: div with GkAmnd class or first styled div
                _title_divs = _a.xpath('.//div[contains(@class, "GkAmnd")]')
                if not _title_divs:
                    _title_divs = _a.xpath('.//div[@style]')
                _title = _title_divs[0].text_content().strip() if _title_divs else ''
                if not _title:
                    continue

                # Snippet: div with F0FGWb class
                _snip_divs = _a.xpath('.//div[contains(@class, "F0FGWb")]')
                _content = _snip_divs[0].text_content().strip() if _snip_divs else ''

                # Meta info (followers, source URL)
                _meta_spans = _a.xpath('.//span[contains(@class, "nC62wb")]')
                _meta = _meta_spans[0].text_content().strip() if _meta_spans else ''

                # Combine: title · meta · snippet (mimics the rich GSA format)
                if _meta and _content:
                    _full_content = _title + ' · ' + _meta + ' · ' + _content
                elif _content:
                    _full_content = _content
                elif _meta:
                    _full_content = _title + ' · ' + _meta
                else:
                    _full_content = ''

                # Clean title: remove source prefix like "LinkedIn · "
                _clean_title = _content if _content else _title

                results.append({
                    'url': _url,
                    'title': _clean_title,
                    'content': _full_content,
                })
            except Exception:
                continue

        if len(results) > 0:
            _log.info("Patch 3: fallback parser found %d results", len(results))

    return results"""

rindex = code.rfind(old2)
if rindex >= 0 and rindex > len(code) // 2:
    code = code[:rindex] + new2 + code[rindex + len(old2):]
    print("PATCH 2 + 3: LinkedIn location enrichment + fallback parser added")
else:
    failed.append("2+3 (LinkedIn location + fallback parser)")

if failed:
    print("FAILED: patch(es) did not apply: " + ", ".join(failed), file=sys.stderr)
    sys.exit(1)

with open(google_py, "w") as f:
    f.write(code)
print("SUCCESS: all patches applied or upstream")
