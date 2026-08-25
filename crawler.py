# -*- coding: utf-8 -*-
"""
예스24 신간(국내도서) 중 판매지수 1000+ 를 모아 data.json 으로 만드는 크롤러.

흐름:
  1) 분야별로 예스24 신상품(attentionnewproduct) 목록을 페이지 단위로 수집
  2) 판매지수 1000 이상인 책만 남김
  3) 그 책들만 예스24 상세페이지에서 ISBN13 추출
  4) ISBN으로 교보문고 통합검색 → 종이책 상세페이지 링크 추출
  5) 교보문고 상세페이지의 related 배열에 'E'로 시작하는 ID가 있으면 전자책 있음으로 판정
  6) 이전에 저장해둔 data.json 이 있으면 그 값과 비교해서 prevSalesIndex 채움
  7) data.json 으로 저장 (index.html이 fetch로 읽는 그 파일)

실행:
  pip install requests beautifulsoup4
  python crawler.py
"""

import json
import os
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.yes24.com/",
}

SALES_INDEX_THRESHOLD = 1000
PAGE_SIZE = 120  # 한 번에 최대한 많이 가져와서 요청 수를 줄임
REQUEST_DELAY = 0.5  # 요청 사이 최소 대기(초) - 서버 부담/차단 방지
DATA_JSON_PATH = "data.json"

# 예스24 국내도서(001001xxx) 분야 코드 - 신상품 페이지 왼쪽 메뉴에서 확인한 값
CATEGORIES = {
    "001001001": "가정 살림",
    "001001011": "건강 취미",
    "001001025": "경제 경영",
    "001001004": "국어 외국어 사전",
    "001001022": "사회 정치",
    "001001046": "소설/시/희곡",
    "001001016": "어린이",
    "001001047": "에세이",
    "001001009": "여행",
    "001001010": "역사",
    "001001007": "예술",
    "001001019": "인문",
    "001001020": "인물",
    "001001026": "자기계발",
    "001001002": "자연과학",
    "001001021": "종교",
    "001001005": "청소년",
    "001001003": "IT 모바일",
}


def fetch(url, **kwargs):
    """공통 GET 요청 (실패 시 None 반환, 예외로 전체가 멈추지 않게)"""
    try:
        res = requests.get(url, headers=HEADERS, timeout=15, **kwargs)
        res.raise_for_status()
        time.sleep(REQUEST_DELAY)
        return res
    except requests.RequestException as e:
        print(f"  ! 요청 실패: {url} ({e})")
        return None


def crawl_category_list(category_code):
    """분야 하나의 신상품 목록을 페이지 넘겨가며 수집."""
    items = []
    page = 1
    while True:
        url = (
            "https://www.yes24.com/product/category/attentionnewproduct"
            f"?pageNumber={page}&pageSize={PAGE_SIZE}&categoryNumber={category_code}"
        )
        res = fetch(url)
        if res is None:
            break

        soup = BeautifulSoup(res.text, "html.parser")
        blocks = soup.select(".item_info")

        if page == 1:
            # 진단용 로그: 응답이 정상 목록 페이지인지 바로 알 수 있게
            print(
                f"    [디버그] status={res.status_code} "
                f"응답길이={len(res.text)} item_info매칭={len(blocks)}"
            )
            if not blocks:
                snippet = re.sub(r"\s+", " ", res.text)[:300]
                print(f"    [디버그] 응답 앞부분: {snippet}")

        if not blocks:
            break

        for b in blocks:
            title_tag = b.select_one("a.gd_name")
            if not title_tag:
                continue
            href = title_tag.get("href", "")
            m = re.search(r"/product/goods/(\d+)", href)
            if not m:
                continue
            goods_id = m.group(1)
            title = title_tag.get_text(strip=True)

            author_tag = b.select_one(".info_auth")
            publisher_tag = b.select_one(".info_pub")
            author = author_tag.get_text(strip=True) if author_tag else ""
            publisher = publisher_tag.get_text(strip=True) if publisher_tag else ""

            sale_tag = b.select_one(".saleNum")
            sale_index = 0
            if sale_tag:
                digits = re.search(r"\d+", sale_tag.get_text())
                if digits:
                    sale_index = int(digits.group())

            items.append(
                {
                    "goodsId": goods_id,
                    "title": title,
                    "author": author,
                    "publisher": publisher,
                    "categoryCode": category_code,
                    "salesIndex": sale_index,
                }
            )

        # 이번 페이지가 PAGE_SIZE 보다 적게 나왔으면 마지막 페이지
        if len(blocks) < PAGE_SIZE:
            break
        page += 1

    return items


def fetch_isbn(goods_id):
    """예스24 상세페이지에서 ISBN13 추출."""
    url = f"https://www.yes24.com/product/goods/{goods_id}"
    res = fetch(url)
    if res is None:
        return None

    soup = BeautifulSoup(res.text, "html.parser")
    for th in soup.select("th"):
        if th.get_text(strip=True) == "ISBN13":
            td = th.find_next_sibling("td")
            if td:
                isbn = re.sub(r"\D", "", td.get_text())
                return isbn or None
    return None


def check_kyobo_ebook(isbn):
    """
    ISBN으로 교보문고 검색 -> 종이책 상세페이지 링크 추출
    -> 상세페이지의 related 배열에서 'E'로 시작하는 ID 있는지 확인.
    반환: (ebook_available: bool|None, kyobo_url: str|None)
    """
    search_url = (
        f"https://search.kyobobook.co.kr/search?keyword={isbn}"
        "&gbCode=TOT&target=total"
    )
    res = fetch(search_url)
    if res is None:
        return None, None

    m = re.search(r"/detail/S\d+", res.text)
    if not m:
        return None, None  # 교보에 이 ISBN으로 매칭되는 종이책이 없음

    kyobo_url = "https://product.kyobobook.co.kr" + m.group()
    detail_res = fetch(kyobo_url)
    if detail_res is None:
        return None, kyobo_url

    rel = re.search(r'"related":\[([^\]]*)\]', detail_res.text)
    if not rel:
        return False, kyobo_url

    has_ebook = bool(re.search(r'"E\d+"', rel.group(1)))
    return has_ebook, kyobo_url


def load_previous_data():
    """직전 data.json 을 읽어서 goodsId -> salesIndex 맵을 만듦 (없으면 빈 dict)."""
    if not os.path.exists(DATA_JSON_PATH):
        return {}
    try:
        with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
            prev = json.load(f)
        books = prev.get("books", prev if isinstance(prev, list) else [])
        return {b["goodsId"]: b.get("salesIndex") for b in books if "goodsId" in b}
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! 이전 data.json 읽기 실패, 무시하고 진행: {e}")
        return {}


def main():
    prev_map = load_previous_data()

    print("== 1) 분야별 신상품 목록 수집 ==")
    all_items = []
    for code, name in CATEGORIES.items():
        print(f"  - {name} ({code}) 수집 중...")
        items = crawl_category_list(code)
        print(f"    {len(items)}건")
        all_items.extend(items)

    print(f"\n총 수집: {len(all_items)}건")

    print("\n== 2) 판매지수 1000+ 필터링 ==")
    filtered = [b for b in all_items if b["salesIndex"] >= SALES_INDEX_THRESHOLD]
    print(f"기준 통과: {len(filtered)}건")

    print("\n== 3~4) ISBN 추출 + 교보문고 전자책 유무 확인 ==")
    result = []
    for i, book in enumerate(filtered, 1):
        print(f"  [{i}/{len(filtered)}] {book['title']}")

        isbn = fetch_isbn(book["goodsId"])
        ebook_available, kyobo_url = (None, None)
        if isbn:
            ebook_available, kyobo_url = check_kyobo_ebook(isbn)

        prev_index = prev_map.get(book["goodsId"])

        result.append(
            {
                "goodsId": book["goodsId"],
                "title": book["title"],
                "author": book["author"],
                "publisher": book["publisher"],
                "categoryCode": book["categoryCode"],
                "isbn": isbn,
                "ebookAvailable": ebook_available,
                "kyoboUrl": kyobo_url,
                "salesIndex": book["salesIndex"],
                "prevSalesIndex": prev_index,
            }
        )

    output = {
        "baseDate": date.today().isoformat(),
        "books": result,
    }

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n완료: {DATA_JSON_PATH} 에 {len(result)}건 저장")


if __name__ == "__main__":
    main()
