from __future__ import annotations
"""한국투자증권 KIS Open Trading API 클라이언트.

REST API 기반으로 주식 시세 조회, 매수/매도 주문, 계좌 잔고 조회 등을 제공.
실전/모의투자 모두 지원.

API 문서: https://apiportal.koreainvestment.com/
"""

import logging
import os
import time
from datetime import datetime, timedelta

import requests
import pandas as pd

from zusik import paths

logger = logging.getLogger(__name__)

# 기본 URL
BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
BASE_URL_VIRTUAL = "https://openapivts.koreainvestment.com:29443"


class KISClient:
    """한국투자증권 KIS API 클라이언트."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        account_no: str,
        account_prod: str = "01",
        is_virtual: bool = False,
    ):
        """
        Args:
            app_key: 앱 키
            app_secret: 앱 시크릿
            account_no: 계좌번호 (8자리, 예: "50123456")
            account_prod: 계좌 상품코드 (기본 "01")
            is_virtual: True면 모의투자, False면 실전투자
        """
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_no = account_no
        self.account_prod = account_prod
        self.is_virtual = is_virtual
        self.base_url = BASE_URL_VIRTUAL if is_virtual else BASE_URL_REAL

        self._access_token = ""
        self._token_expires = datetime.min

        # 주문 관문 안전 검증 (변조/조작 방어) — 모든 주문이 _order/_us_order 통과
        from zusik.core.resilience import OrderSafetyValidator
        self._order_safety = OrderSafetyValidator()
        self._last_order_ts: dict = {}   # code -> (side, ts) — 워시트레이딩 검증용
        self._buying_power_cache: dict = {}

    # ── 인증 ──

    # 레포 루트 기준 절대 경로 — CWD 가 달라져도(스크립트/테스트/수동 실행) 토큰 캐시가
    # data/data/kis_token.json 같은 중복 위치에 생기지 않게 한다.
    _TOKEN_FILE = paths.data_path("kis_token.json")

    def _ensure_token(self):
        """액세스 토큰이 없거나 만료 임박하면 재발급. 파일 캐시로 중복 발급 방지."""
        import json as _json

        # 메모리 캐시 확인
        if self._access_token and datetime.now() < self._token_expires - timedelta(hours=1):
            return

        # 파일 캐시 확인 (다른 프로세스가 발급한 토큰 재사용)
        try:
            if os.path.exists(self._TOKEN_FILE):
                with open(self._TOKEN_FILE, encoding="utf-8") as f:
                    cached = _json.load(f)
                expires = datetime.strptime(cached["expires"], "%Y-%m-%d %H:%M:%S")
                if datetime.now() < expires - timedelta(hours=1):
                    self._access_token = cached["token"]
                    self._token_expires = expires
                    logger.info("KIS 토큰 캐시 사용 (만료: %s)", expires)
                    return
        except Exception:
            pass

        # 신규 발급
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        expires_str = data.get("access_token_token_expired", "")
        if expires_str:
            self._token_expires = datetime.strptime(expires_str, "%Y-%m-%d %H:%M:%S")
        else:
            self._token_expires = datetime.now() + timedelta(hours=23)

        # 파일 캐시 저장 — 토큰은 시크릿이므로 소유자 전용(0600)으로 강제.
        try:
            os.makedirs(os.path.dirname(self._TOKEN_FILE), exist_ok=True)
            with open(self._TOKEN_FILE, "w", encoding="utf-8") as f:
                _json.dump({"token": self._access_token, "expires": self._token_expires.strftime("%Y-%m-%d %H:%M:%S")}, f)
            try:
                os.chmod(self._TOKEN_FILE, 0o600)
            except OSError:
                pass
        except Exception:
            pass

        logger.info("KIS 토큰 발급 완료 (만료: %s)", self._token_expires)

    def _headers(self, tr_id: str) -> dict:
        """API 호출 공통 헤더."""
        self._ensure_token()
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }

    def _rate_limit(self, is_order: bool = False):
        """초당 호출 제한 — 최대한 한도에 맞춰 빠르게.

        모의투자(is_virtual): 초당 1건 한도 → 0.85건/초로 운용 (계정 성숙과 무관, 항상)
        실전 신규 3일: 초당 3건 → 초당 2.5건으로 운용 (안전마진 15%)
        실전 3일 이후: 초당 20건 → 초당 18건으로 운용

        한도 상향 판정 (실전만):
          1) 환경변수 `KIS_API_MATURE=true` → 즉시 18 req/sec
          2) `data/kis_api_start.txt` 파일에 최초 날짜 영속화 → 재시작에도 유지

        주문 슬롯 보호 (키움 ch8 패턴):
          전체 한도와 별개로 주문 TR은 `_order_call_times`로 1초당 최대 4건만 통과.
          조회 폭주(스크리닝/MTF 분석 등)가 한도를 잡고 있어도 매도 주문이 큐 끝에서
          기다리지 않게 하기 위함. 4건은 신규/성숙 양쪽 다 안전한 수치.
        """
        import os, threading
        #: 스레드 안전성 — 코어 패스/분석/자산동기화 등 여러 스레드가 동시에
        # _call_times를 race해 스로틀을 우회 → 초당 한도 초과(EGW00201) 유발하던 문제.
        # 락으로 직렬화해 실제 한도를 강제 (sleep도 락 안에서 — 각 스레드가 슬롯을 순서대로 대기).
        if not hasattr(self, "_rate_lock"):
            self._rate_lock = threading.Lock()
        with self._rate_lock:
            if not hasattr(self, "_call_times"):
                self._call_times = []
                self._order_call_times = []
                # env 우선 — 계정이 이미 3일 넘었으면 true로 세팅
                if os.getenv("KIS_API_MATURE", "").lower() in ("true", "1", "yes"):
                    self._api_start_date = datetime.min
                else:
                    # 파일 영속화 (재시작해도 최초 날짜 유지)
                    start_file = os.path.join("data", "kis_api_start.txt")
                    try:
                        if os.path.exists(start_file):
                            with open(start_file, encoding="utf-8") as f:
                                self._api_start_date = datetime.fromisoformat(f.read().strip())
                        else:
                            self._api_start_date = datetime.now()
                            os.makedirs("data", exist_ok=True)
                            with open(start_file, "w", encoding="utf-8") as f:
                                f.write(self._api_start_date.isoformat())
                    except Exception:
                        self._api_start_date = datetime.now()

            if getattr(self, "is_virtual", False):
                # 모의투자는 초당 1건만 허용 — 성숙/미성숙 무관하게 고정.
                # 균등 간격(아래 min_interval)이 실질 강제라 0.85건/초 = 호출 간 ~1.18초.
                max_per_sec = 0.85
            else:
                days_since_start = (datetime.now() - self._api_start_date).days
                if days_since_start < 3:
                    max_per_sec = 2.5
                else:
                    #: 18 → 12 (EGW00201 반복 — 안전마진 확대).
                    max_per_sec = 12

            # 균등 간격 강제: 슬라이딩 윈도우만으론 "N콜을 수십ms에 몰아치고 대기"가
            # 가능 → KIS가 미세 버스트를 '초당 거래건수 초과(EGW00201)'로 거부. 호출 사이 최소
            # 간격을 둬 고르게 분산(락 안이라 스레드 간에도 직렬·균등).
            min_interval = 1.0 / max_per_sec
            since_last = time.time() - getattr(self, "_last_call_ts", 0.0)
            if since_last < min_interval:
                time.sleep(min_interval - since_last)

            now = time.time()
            self._call_times = [t for t in self._call_times if now - t < 1.0]
            if len(self._call_times) >= max_per_sec:
                wait = 1.0 - (now - self._call_times[0]) + 0.05
                if wait > 0:
                    time.sleep(wait)

            if is_order:
                now2 = time.time()
                self._order_call_times = [t for t in self._order_call_times if now2 - t < 1.0]
                if len(self._order_call_times) >= 4:
                    wait2 = 1.0 - (now2 - self._order_call_times[0]) + 0.05
                    if wait2 > 0:
                        time.sleep(wait2)
                self._order_call_times.append(time.time())

            self._call_times.append(time.time())
            self._last_call_ts = time.time()

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        """GET 요청 (자동 재시도). 429 rate limit + HTTP 에러 본문 포함."""
        return self._request("GET", path, tr_id, params=params)

    def _post(self, path: str, tr_id: str, body: dict) -> dict:
        """POST 요청 (자동 재시도). 429 rate limit + HTTP 에러 본문 포함."""
        return self._request("POST", path, tr_id, body=body)

    def _request(self, method: str, path: str, tr_id: str,
                 params: dict | None = None, body: dict | None = None,
                 is_order: bool = False) -> dict:
        """HTTP 요청 통합 — 재시도 + rate limit + 에러 본문 포함.

        키움 REST API 예제(utils.py)에서 배운 패턴: HTTPError 시 response.text를
        예외 메시지에 포함해 디버그 용이하게. KIS API의 잘못된 종목코드/rate limit
        등 에러 본문이 중요한 정보를 담고 있음.

        is_order=True면 주문 전용 rate slot까지 함께 점유 (조회 폭주에도
        주문이 1초당 최소 4건은 통과하도록 보호).
        """
        self._rate_limit(is_order=is_order)
        url = f"{self.base_url}{path}"
        last_err = None
        resp = None  # 초기화: HTTPError 핸들러가 resp 미할당 시 UnboundLocalError 방지
        for attempt in range(3):
            try:
                if method == "GET":
                    resp = requests.get(url, headers=self._headers(tr_id),
                                        params=params, timeout=10)
                else:
                    resp = requests.post(url, headers=self._headers(tr_id),
                                         json=body, timeout=10)
                resp.raise_for_status()
                return resp.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(1 * (attempt + 1))
                    logger.debug("KIS %s 재시도 (%d/3): %s", method, attempt + 2, path)
            except requests.HTTPError as e:
                # resp가 None인 경우(이론상 거의 없음 — requests.get 자체가 HTTPError 직접 발생 X)
                # 안전 폴백: 원본 예외 그대로 raise
                if resp is None:
                    raise
                body_preview = (resp.text or "")[:500]
                # rate limit 재시도: 429 + EGW00201(원장 초당 거래건수 초과, HTTP 500으로 옴).
                #: EGW00201이 재시도 안 돼 KR 매매가 크래시하던 문제 — backoff 재시도.
                rate_limited = (resp.status_code == 429
                                or "EGW00201" in body_preview
                                or "초당 거래" in body_preview)
                if rate_limited and attempt < 2:
                    self._rate_limit()  # 추가 스로틀 후
                    time.sleep(1.0 * (attempt + 1))
                    logger.debug("KIS rate limit(%s) 재시도 (%d/3): %s",
                                 resp.status_code, attempt + 2, path)
                    last_err = e
                    continue
                # 그 외 HTTP 에러는 응답 본문까지 포함해 raise (디버그 용이)
                raise requests.HTTPError(
                    f"HTTP {resp.status_code} at {path}: {body_preview}"
                ) from e
        raise last_err

    # ── 시세 조회 ──

    def get_current_price(self, stock_code: str) -> dict:
        """주식 현재가 조회.

        Returns:
            {"price": 현재가, "change_rate": 등락률, "volume": 거래량, ...}
        """
        tr_id = "FHKST01010100"
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
        }
        data = self._get("/uapi/domestic-stock/v1/quotations/inquire-price", tr_id, params)
        output = data.get("output", {})

        return {
            "price": int(output.get("stck_prpr", 0)),
            "change_rate": float(output.get("prdy_ctrt", 0)),
            "volume": int(output.get("acml_vol", 0)),
            "high": int(output.get("stck_hgpr", 0)),
            "low": int(output.get("stck_lwpr", 0)),
            "open": int(output.get("stck_oprc", 0)),
            "prev_close": int(output.get("stck_sdpr", 0)),
            "market_cap": int(output.get("hts_avls", 0)),
            "per": float(output.get("per", 0)),
            "pbr": float(output.get("pbr", 0)),
            "name": output.get("hts_kor_isnm", stock_code),
        }

    def get_ohlcv(
        self, stock_code: str, period: str = "D", count: int = 100
    ) -> pd.DataFrame | None:
        """일/주/월 OHLCV 데이터 조회.

        Args:
            stock_code: 종목코드 (예: "005930")
            period: "D"=일봉, "W"=주봉, "M"=월봉
            count: 조회할 봉 수

        Returns:
            DataFrame with columns: open, high, low, close, volume
        """
        tr_id = "FHKST01010400"
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")

        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
            "fid_input_date_1": start_date,
            "fid_input_date_2": end_date,
            "fid_period_div_code": period,
            "fid_org_adj_prc": "0",  # 수정주가
        }
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-price", tr_id, params
        )
        output = data.get("output", [])

        if not output:
            return None

        rows = []
        for item in output:
            try:
                rows.append({
                    "date": pd.to_datetime(item["stck_bsop_date"]),
                    "open": int(item["stck_oprc"]),
                    "high": int(item["stck_hgpr"]),
                    "low": int(item["stck_lwpr"]),
                    "close": int(item["stck_clpr"]),
                    "volume": int(item["acml_vol"]),
                })
            except (KeyError, ValueError):
                continue

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df = df.set_index("date").sort_index()
        return df.tail(count)

    def get_daily_long(self, stock_code: str, days: int = 250, period: str = "D") -> pd.DataFrame | None:
        """장기 일봉 — inquire-daily-itemchartprice(FHKST03010100, 100봉/call) 청크 조회.

        get_ohlcv가 쓰는 inquire-daily-price는 최근 ~30봉만 반환해 60일 모멘텀·백테스트에
        부족하다. 이 메서드는 날짜 구간을 뒤로 옮겨가며 합쳐 최대 `days`봉을 만든다 (국내주식).
        """
        tr_id = "FHKST03010100"
        all_rows: dict = {}
        cursor = datetime.now()
        for _ in range((days // 100) + 2):
            end_date = cursor.strftime("%Y%m%d")
            start_date = (cursor - timedelta(days=150)).strftime("%Y%m%d")
            params = {
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": stock_code,
                "fid_input_date_1": start_date,
                "fid_input_date_2": end_date,
                "fid_period_div_code": period,
                "fid_org_adj_prc": "0",
            }
            try:
                data = self._get(
                    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                    tr_id, params,
                )
            except Exception:
                break
            output = data.get("output2", []) or []
            got = 0
            oldest = None
            for item in output:
                ds = item.get("stck_bsop_date")
                if not ds or not item.get("stck_clpr"):
                    continue
                try:
                    all_rows[ds] = {
                        "date": pd.to_datetime(ds),
                        "open": int(item["stck_oprc"]), "high": int(item["stck_hgpr"]),
                        "low": int(item["stck_lwpr"]), "close": int(item["stck_clpr"]),
                        "volume": int(item.get("acml_vol", 0) or 0),
                    }
                    got += 1
                    if oldest is None or ds < oldest:
                        oldest = ds
                except (KeyError, ValueError):
                    continue
            if len(all_rows) >= days or got == 0 or oldest is None:
                break
            cursor = pd.to_datetime(oldest) - timedelta(days=1)
        if not all_rows:
            return None
        df = pd.DataFrame(list(all_rows.values())).set_index("date").sort_index()
        return df.tail(days)

    def get_minute_ohlcv(self, stock_code: str, minutes: int = 60) -> pd.DataFrame | None:
        """분봉 OHLCV 데이터 조회.

        Args:
            stock_code: 종목코드
            minutes: 1, 3, 5, 10, 15, 30, 60
        """
        tr_id = "FHKST01010800"
        now = datetime.now().strftime("%H%M%S")

        params = {
            "fid_etc_cls_code": "",
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
            "fid_input_hour_1": now,
            "fid_pw_data_incu_yn": "Y",
        }
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-dailyprice", tr_id, params
        )
        output = data.get("output", [])

        if not output:
            return None

        rows = []
        for item in output:
            try:
                rows.append({
                    "date": pd.to_datetime(
                        item.get("stck_bsop_date", "") + item.get("stck_cntg_hour", ""),
                        format="%Y%m%d%H%M%S",
                    ),
                    "open": int(item.get("stck_oprc", 0)),
                    "high": int(item.get("stck_hgpr", 0)),
                    "low": int(item.get("stck_lwpr", 0)),
                    "close": int(item.get("stck_prpr", 0)),
                    "volume": int(item.get("cntg_vol", 0)),
                })
            except (KeyError, ValueError):
                continue

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df = df.set_index("date").sort_index()
        return df

    # ── 계좌 ──

    # 잔고 조회 TTL 캐시 — bot 한 사이클에서 51회 호출되는 핫경로.
    # 5초 캐시로 사이클 내 중복 호출 흡수. 매매 직후엔 사용자가 invalidate 호출.
    _BALANCE_TTL_SEC = 5

    def invalidate_balance_cache(self):
        """매매 직후 호출해서 다음 read에 신선한 값 보장."""
        if hasattr(self, "_balance_cache"):
            self._balance_cache = {}
        if hasattr(self, "_us_balance_cache"):
            self._us_balance_cache = {}
        if hasattr(self, "_buying_power_cache"):
            self._buying_power_cache = {}

    def get_balance(self) -> dict:
        """계좌 잔고 조회 (5초 TTL 캐시).

        Returns:
            {
                "cash": 예수금(원),
                "total_eval": 총평가금액,
                "total_profit": 총손익,
                "total_profit_rate": 총수익률(%),
                "holdings": [{"code", "name", "qty", "avg_price", "current_price", "profit_rate"}, ...]
            }
        """
        cache = getattr(self, "_balance_cache", None)
        now_ts = time.time()
        if cache and (now_ts - cache.get("ts", 0) < self._BALANCE_TTL_SEC):
            return cache["data"]
        try:
            result = self._fetch_balance_uncached()
        except Exception as e:
            # KIS 서버 500/연결 폭주(폭락일 과부하) 시 직전 캐시로 폴백 — 종목 실행 전체가
            # 죽던 버그 차단. 캐시가 아예 없을 때만 예외 전파.
            if cache and cache.get("data"):
                logger.warning("KR 잔고 조회 실패 → 직전 캐시 사용(%.0fs 경과): %s",
                               now_ts - cache.get("ts", 0), str(e)[:120])
                return cache["data"]
            raise
        self._balance_cache = {"ts": now_ts, "data": result}
        return result

    def _fetch_balance_uncached(self) -> dict:
        tr_id = "VTTC8434R" if self.is_virtual else "TTTC8434R"
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_prod,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance", tr_id, params
        )

        holdings = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty <= 0:
                continue
            holdings.append({
                "code": item.get("pdno", ""),
                "name": item.get("prdt_name", ""),
                "qty": qty,
                "avg_price": int(float(item.get("pchs_avg_pric", 0))),
                "current_price": int(item.get("prpr", 0)),
                "eval_amount": int(item.get("evlu_amt", 0)),
                "profit": int(item.get("evlu_pfls_amt", 0)),
                "profit_rate": float(item.get("evlu_pfls_rt", 0)),
            })

        summary = data.get("output2", [{}])
        if isinstance(summary, list):
            summary = summary[0] if summary else {}

        # 주문가능금액 별도 조회. KIS 공식 명세상 현금 주문은
        # nrcvb_buy_amt(미수없는매수금액)를 사용해야 한다. dnca_tot_amt는 예수금이며
        # 종목 증거금률/미체결 주문을 반영한 실제 매수가능금액이 아니므로 매수 게이트에
        # 사용하지 않는다. 조회 실패 시 0으로 fail-closed하고 총자산에는 예수금을 보존한다.
        deposit_cash = int(summary.get("dnca_tot_amt", 0) or 0)
        orderable_cash = 0
        cash_verified = False
        try:
            if holdings:
                ref_code = holdings[0]["code"]
                ref_price = int(holdings[0].get("current_price", 0) or 0)
            else:
                ref_code = "005930"
                ref_price = int(self.get_current_price(ref_code).get("price", 0) or 0)
            orderable_cash = self._get_orderable_cash(ref_code, ref_price)
            cash_verified = True
        except Exception as e:
            logger.warning("KR 주문가능금액 조회 실패 → 신규 매수 fail-closed: %s", str(e)[:120])

        # 미정산 매도 대금 (T+2 결제 대기) — 진짜 총자산 계산용
        # dnca_tot_amt: 즉시 가용 (D+0)
        # nxdy_excc_amt: D+1 결제 예정 (전일 매도)
        # prvs_rcdl_excc_amt: D+2 결제 예정 (전전일 + 미정산 합계)
        nxdy_cash = int(summary.get("nxdy_excc_amt", 0) or 0)
        d2_cash = int(summary.get("prvs_rcdl_excc_amt", 0) or 0)
        # 진짜 총 cash = max(즉시 + 미정산)
        # prvs_rcdl_excc_amt가 D+2 결제 후 잔고이므로 가장 정확
        total_cash = max(deposit_cash, d2_cash, nxdy_cash)

        return {
            "cash": orderable_cash,           # 매수 게이트용 (즉시 가용)
            "cash_verified": cash_verified,   # False면 매수 금지, 표시/자산은 total_cash 사용
            "deposit_cash": deposit_cash,     # 단순 예수금(주문가능금액과 다를 수 있음)
            "total_cash": total_cash,         # equity 계산용 (미정산 포함)
            "nxdy_cash": nxdy_cash,           # D+1 결제 예정
            "d2_cash": d2_cash,               # D+2 결제 예정
            "total_eval": int(summary.get("scts_evlu_amt", 0)),
            "total_profit": int(summary.get("evlu_pfls_smtl_amt", 0)),
            "total_profit_rate": float(summary.get("tot_evlu_pfls_rt", 0)),
            "holdings": holdings,
        }

    _BUYING_POWER_TTL_SEC = 2

    def get_orderable_buy_info(self, stock_code: str, reference_price: int,
                               *, force: bool = False) -> dict:
        """종목별 현금 매수가능금액/수량을 조회한다.

        KIS ``inquire-psbl-order`` 명세에 따라 미수 없는 현금 주문은
        ``nrcvb_buy_amt``/``nrcvb_buy_qty``를 사용한다. 특히 가능 수량은 실제 주문과
        동일한 시장가(``ORD_DVSN=01``) 조건으로 조회해 종목 증거금률을 반영한다.
        응답 필드가 없거나 조회가 실패하면 호출자에게 예외를 전달해 매수를 fail-closed한다.
        """
        code = str(stock_code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            raise ValueError(f"잘못된 KR 종목코드: {stock_code!r}")
        price = int(reference_price or 0)
        if price <= 0:
            price = int(self.get_current_price(code).get("price", 0) or 0)
        if price <= 0:
            raise ValueError(f"{code} 매수가능조회 기준가격 없음")

        now = time.time()
        cache = getattr(self, "_buying_power_cache", {}) or {}
        cached = cache.get(code)
        if (not force and cached
                and now - float(cached.get("ts", 0)) < self._BUYING_POWER_TTL_SEC
                and abs(int(cached.get("reference_price", 0)) - price)
                <= max(1, int(price * 0.01))):
            return dict(cached["data"])

        tr_id = "VTTC8908R" if self.is_virtual else "TTTC8908R"
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_prod,
            "PDNO": code,
            "ORD_UNPR": str(price),
            "ORD_DVSN": "01",  # 시장가
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order", tr_id, params
        )
        output = data.get("output", {})
        if "nrcvb_buy_amt" not in output or "nrcvb_buy_qty" not in output:
            raise RuntimeError("KIS 매수가능조회 응답에 nrcvb 필드 없음")
        info = {
            "cash": int(float(output.get("nrcvb_buy_amt", 0) or 0)),
            "max_qty": int(float(output.get("nrcvb_buy_qty", 0) or 0)),
            "reference_price": price,
            "stock_code": code,
        }
        if not hasattr(self, "_buying_power_cache"):
            self._buying_power_cache = {}
        self._buying_power_cache[code] = {
            "ts": now, "reference_price": price, "data": dict(info),
        }
        return info

    def _get_orderable_cash(self, stock_code: str = "005930",
                            reference_price: int = 0) -> int:
        """미수 없는 실제 주문가능현금 조회(잔고 계약 호환용)."""
        return int(self.get_orderable_buy_info(stock_code, reference_price)["cash"])

    # ── 주문 가격 정합화 (호가단위) ──
    # 키움 ch8 예제 `get_tick_size`/`get_order_price` 그대로 적용.
    # KR 주식은 가격대별 호가단위가 있어서 비정합 가격은 silent reject 됨.
    # AI 분석이 "21,733원 매수" 같은 가격을 내면 21,700원으로 내림 정렬.

    @staticmethod
    def get_tick_size(price: int) -> int:
        """가격대별 호가 단위(원) 반환."""
        p = int(price)
        if p < 2_000:        return 1
        if p < 5_000:        return 5
        if p < 20_000:       return 10
        if p < 50_000:       return 50
        if p < 200_000:      return 100
        if p < 500_000:      return 500
        return 1_000

    @classmethod
    def align_to_tick(cls, price: int, direction: str = "down") -> int:
        """호가단위에 맞게 가격 정렬.

        direction:
          "down" — 내림 (매수 시 보수적, 기본)
          "up"   — 올림 (매도 시 보수적)
          "near" — 가장 가까운 호가
        """
        p = int(price)
        if p <= 0:
            return p
        tick = cls.get_tick_size(p)
        if direction == "up":
            return p + ((-p) % tick)
        if direction == "near":
            r = p % tick
            return p - r if r * 2 < tick else p + (tick - r)
        # down
        return p - (p % tick)

    # ── 주문 ──

    def buy_market(self, stock_code: str, qty: int, reference_price: int = 0) -> dict:
        """시장가 매수.

        Args:
            stock_code: 종목코드
            qty: 매수 수량
        """
        requested_qty = int(qty) if isinstance(qty, int) and not isinstance(qty, bool) else qty
        try:
            info = self.get_orderable_buy_info(stock_code, reference_price)
        except Exception as e:
            logger.warning("매수 사전 차단 (%s): 종목별 주문가능수량 조회 실패 — %s",
                           stock_code, str(e)[:120])
            return {
                "success": False,
                "message": f"주문가능수량 조회 실패: {e}",
                "order_no": "",
                "blocked": True,
                "requested_qty": requested_qty,
                "submitted_qty": 0,
            }

        max_qty = int(info.get("max_qty", 0) or 0)
        if (not isinstance(requested_qty, int) or isinstance(requested_qty, bool)
                or requested_qty <= 0 or max_qty <= 0):
            return {
                "success": False,
                "message": f"종목별 주문가능수량 0주 (요청 {requested_qty}주)",
                "order_no": "",
                "blocked": True,
                "requested_qty": requested_qty,
                "submitted_qty": 0,
            }
        submitted_qty = min(requested_qty, max_qty)
        if submitted_qty < requested_qty:
            logger.info("매수 수량 자동 감액: %s %d주 → %d주 (KIS 현금매수 가능수량)",
                        stock_code, requested_qty, submitted_qty)

        result = self._order(
            stock_code, "buy", submitted_qty, price=0, order_type="01",
            buy_preflight=info, market_price=int(info.get("reference_price", 0) or 0),
        )

        # 조회 직후에도 미체결 주문/증거금 변동으로 거절될 수 있다. 같은 수량을 반복하지 않고
        # 주문가능수량을 강제 재조회한 뒤 더 작은 수량으로 단 한 번만 재시도한다.
        if (not result.get("success")
                and "주문가능금액" in str(result.get("message", ""))
                and submitted_qty > 1):
            try:
                if hasattr(self, "_buying_power_cache"):
                    self._buying_power_cache.pop(stock_code, None)
                fresh = self.get_orderable_buy_info(stock_code, reference_price, force=True)
                retry_qty = min(submitted_qty - 1, int(fresh.get("max_qty", 0) or 0))
                if retry_qty > 0:
                    logger.info("주문가능금액 거절 후 감량 재시도: %s %d주 → %d주",
                                stock_code, submitted_qty, retry_qty)
                    result = self._order(
                        stock_code, "buy", retry_qty, price=0, order_type="01",
                        buy_preflight=fresh,
                        market_price=int(fresh.get("reference_price", 0) or 0),
                    )
                    submitted_qty = retry_qty
            except Exception as e:
                logger.warning("주문가능금액 거절 후 재조회 실패 (%s): %s", stock_code, str(e)[:120])

        result["requested_qty"] = requested_qty
        result["submitted_qty"] = submitted_qty if result.get("success") else 0
        return result

    def sell_market(self, stock_code: str, qty: int) -> dict:
        """시장가 매도."""
        return self._order(stock_code, "sell", qty, price=0, order_type="01")

    def _order(self, stock_code: str, side: str, qty: int, price: int, order_type: str,
               buy_preflight: dict | None = None, market_price: int = 0) -> dict:
        """주문 실행.

        order_type: "00"=지정가, "01"=시장가
        """
        if side == "buy":
            tr_id = "VTTC0802U" if self.is_virtual else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self.is_virtual else "TTTC0801U"

        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_prod,
            "PDNO": stock_code,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }

        logger.info(
            "%s 주문: %s %s주 (유형: %s, 가격: %s)",
            "매수" if side == "buy" else "매도",
            stock_code, qty,
            "시장가" if order_type == "01" else f"지정가 {price:,}원",
            price if order_type == "00" else "시장가",
        )

        # 검증용 사전 잔고 스냅샷 (KR만 — US는 T+2 지연 발생). 캐시된 값이라 추가 API 호출 없음
        pre_qty_snapshot = 0
        held_qty = None
        orderable_cash = None
        if stock_code.isdigit() and len(stock_code) == 6:
            try:
                bal_pre = self.get_balance()
                orderable_cash = (buy_preflight or {}).get("cash", bal_pre.get("cash"))
                held_qty = 0
                for h in bal_pre.get("holdings", []):
                    if h.get("code") == stock_code:
                        pre_qty_snapshot = h.get("qty", 0)
                        held_qty = pre_qty_snapshot
                        break
            except Exception:
                pass

        # ── 주문 관문 안전 검증 (변조/조작 방어) — fail-closed, 전송 직전 ──
        # 지정가만 가격 밴드 검증용 시장가 조회 (시장가 주문은 추가 API 호출 없음)
        market_price = float(market_price or 0)
        if order_type == "00" and price and price > 0:
            try:
                market_price = float(self.get_current_price(stock_code).get("price", 0) or 0)
            except Exception:
                market_price = 0.0
        _prev = self._last_order_ts.get(stock_code)
        _last_opp = _prev[1] if (_prev and _prev[0] != side) else 0.0
        _ok, _why = self._order_safety.validate(
            side=side, code=stock_code, qty=qty, price=price, order_type=order_type,
            held_qty=held_qty, orderable_cash=orderable_cash,
            market_price=market_price, last_opposite_ts=_last_opp)
        if not _ok:
            logger.critical("주문 안전 차단 (KR %s %s %s주): %s", side, stock_code, qty, _why)
            return {"success": False, "message": f"안전 검증 차단: {_why}",
                    "order_no": "", "blocked": True}
        self._last_order_ts[stock_code] = (side, time.time())

        data = self._request(
            "POST", "/uapi/domestic-stock/v1/trading/order-cash", tr_id,
            body=body, is_order=True,
        )

        result = {
            "success": data.get("rt_cd") == "0",
            "message": data.get("msg1", ""),
            "order_no": data.get("output", {}).get("ODNO", ""),
        }
        logger.info("주문 결과: %s", result)
        # 주문 성공 시 잔고 캐시 무효화 — 다음 read는 신선한 값
        if result["success"]:
            self.invalidate_balance_cache()
            # 검증: KIS rt_cd=0은 "주문 전송 완료"일 뿐 체결 보장 아님.
            # 폭락일 인버스 3종 + 현대차 매도 5건이 "전송 완료" 응답에도 미체결로 거부됨에
            # bot이 phantom position을 기록해 추가 헷지 매수가 "분할 매수 완료"로 차단되는 사고.
            # 3초 대기 후 잔고 재조회로 실제 변화 확인. KR 종목만 적용 (US는 T+2 결제로 지연 발생).
            if stock_code.isdigit() and len(stock_code) == 6:
                try:
                    time.sleep(3)
                    fresh = self._fetch_balance_uncached()
                    post_qty = 0
                    for h in fresh.get("holdings", []):
                        if h.get("code") == stock_code:
                            post_qty = h.get("qty", 0)
                            break
                    delta = post_qty - pre_qty_snapshot
                    if side == "buy" and delta <= 0:
                        result["success"] = False
                        result["message"] = f"미체결 의심: 매수 후 잔고 변화 없음 (pre={pre_qty_snapshot}, post={post_qty}). KIS 응답='{data.get('msg1', '')}'"
                        logger.warning("주문 검증 실패: %s 매수 %d주 → 잔고 변화 없음 (KIS는 success 반환)", stock_code, qty)
                    elif side == "sell" and delta >= 0:
                        result["success"] = False
                        result["message"] = f"미체결 의심: 매도 후 잔고 변화 없음 (pre={pre_qty_snapshot}, post={post_qty}). KIS 응답='{data.get('msg1', '')}'"
                        logger.warning("주문 검증 실패: %s 매도 %d주 → 잔고 변화 없음 (KIS는 success 반환)", stock_code, qty)
                    else:
                        logger.info("주문 검증 OK: %s %s 잔고 %d→%d (delta %+d)", stock_code, side, pre_qty_snapshot, post_qty, delta)
                except Exception as e:
                    logger.warning("주문 검증 중 예외 (성공으로 처리): %s", e)
        return result

    # ── 정정/취소 ──
    # 키움 ch8 패턴: 지정가 미체결 N초 초과 시 시장가 전환 또는 취소.
    # KIS는 정정/취소를 동일 TR(0803U)로 처리하고 RVSE_CNCL_DVSN_CD로 분기.

    def revise_order(self, stock_code: str, order_no: str, qty: int, price: int,
                     order_type: str = "00", org_branch: str = "") -> dict:
        """미체결 주문 정정.

        order_type: "00"=지정가 정정, "01"=시장가 정정 (실질 시장가 전환)
        price=0이고 order_type="01"이면 시장가 전환.
        지정가 정정 시 가격은 자동으로 호가단위에 정렬됨.
        """
        if order_type == "00" and price > 0:
            price = self.align_to_tick(price, direction="near")
        return self._revise_cancel(stock_code, order_no, qty, price=price,
                                   revise=True, order_type=order_type,
                                   org_branch=org_branch)

    def _revise_cancel(self, stock_code: str, order_no: str, qty: int, price: int,
                       revise: bool, order_type: str = "00",
                       org_branch: str = "") -> dict:
        tr_id = "VTTC0803U" if self.is_virtual else "TTTC0803U"
        # 정정(revise)만 관문 검증 — 취소(revise=False)는 리스크를 줄이는 동작이라 차단 금지.
        # 일반 주문과 같은 단일 관문(OrderSafetyValidator)에서 검증한다.
        if revise:
            _ok, _why = self._order_safety.validate_amend(
                code=stock_code, order_no=order_no, qty=qty,
                price=price, order_type=order_type)
            if not _ok:
                logger.critical("정정 안전 차단 (%s 주문 %s %s주): %s",
                                stock_code, order_no, qty, _why)
                return {"success": False, "message": f"정정 안전 검증 차단: {_why}",
                        "order_no": "", "blocked": True}
        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_prod,
            "KRX_FWDG_ORD_ORGNO": org_branch,
            "ORGN_ODNO": order_no,
            "ORD_DVSN": order_type,
            "RVSE_CNCL_DVSN_CD": "01" if revise else "02",
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": str(int(price)),
            "QTY_ALL_ORD_YN": "N" if qty > 0 else "Y",
            "PDNO": stock_code,
        }
        logger.info(
            "%s 주문: %s 원주문 %s, %s주 %s원",
            "정정" if revise else "취소",
            stock_code, order_no, qty,
            f"{price:,}" if revise and order_type == "00" else "시장가" if order_type == "01" else "취소",
        )
        data = self._request(
            "POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", tr_id,
            body=body, is_order=True,
        )
        return {
            "success": data.get("rt_cd") == "0",
            "message": data.get("msg1", ""),
            "order_no": data.get("output", {}).get("ODNO", ""),
        }

    # ── 호가창 ──
    # 매수 시 best_ask + 1tick 같은 스마트 지정가에 사용.

    def get_orderbook(self, stock_code: str) -> dict | None:
        """호가창 조회 (10단계).

        Returns:
            {"asks": [{"price","qty"} ...10],
             "bids": [{"price","qty"} ...10],
             "best_ask": int, "best_bid": int}
        """
        tr_id = "FHKST01010200"
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
        }
        try:
            data = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                tr_id, params,
            )
        except Exception as e:
            logger.debug("호가 조회 실패 %s: %s", stock_code, str(e)[:80])
            return None
        out = (data.get("output1") or {})
        if not out:
            return None
        asks, bids = [], []
        for i in range(1, 11):
            try:
                ap = int(out.get(f"askp{i}", 0) or 0)
                aq = int(out.get(f"askp_rsqn{i}", 0) or 0)
                bp = int(out.get(f"bidp{i}", 0) or 0)
                bq = int(out.get(f"bidp_rsqn{i}", 0) or 0)
                if ap > 0:
                    asks.append({"price": ap, "qty": aq})
                if bp > 0:
                    bids.append({"price": bp, "qty": bq})
            except (ValueError, TypeError):
                continue
        if not asks or not bids:
            return None
        return {
            "asks": asks,
            "bids": bids,
            "best_ask": asks[0]["price"],
            "best_bid": bids[0]["price"],
        }

    # ── 유틸리티 ──

    @staticmethod
    def market_phase() -> str:
        """현재 장 상태 반환.

        Returns:
            "pre_market"  — 평일 08:30~09:00 (장 시작 전 준비)
            "open"        — 09:00~15:20 (정규장, 동시호가 직전까지)
            "closing"     — 15:20~15:30 (장 마감 동시호가, 주문 위험)
            "post_market" — 15:30~16:00 (장 마감 직후, 리포트 시간)
            "closed"      — 그 외 (장 마감)
        """
        now = datetime.now()
        if now.weekday() >= 5:
            return "closed"

        t = now.hour * 100 + now.minute  # HHMM 정수

        if 830 <= t < 900:
            return "pre_market"
        if 900 <= t < 1520:
            return "open"
        if 1520 <= t < 1530:
            return "closing"
        if 1530 <= t < 1600:
            return "post_market"
        return "closed"

    @staticmethod
    def is_market_open() -> bool:
        """정규 거래 시간인지 확인 (09:00~15:20, 동시호가 제외)."""
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        market_open = now.replace(hour=9, minute=0, second=0)
        market_close = now.replace(hour=15, minute=20, second=0)
        return market_open <= now <= market_close

    @staticmethod
    def minutes_to_close() -> "float | None":
        """KR 정규장 마감(15:20)까지 남은 분. 정규장(09:00~15:20)이 아니면 None.

        인버스 EOD 수익 락인 등 '마감 임박' 판정용. 동시호가(15:20~)는 주문 위험이라 제외.
        """
        now = datetime.now()
        if now.weekday() >= 5:
            return None
        t = now.hour * 100 + now.minute
        if not (900 <= t < 1520):
            return None
        close = now.replace(hour=15, minute=20, second=0, microsecond=0)
        return (close - now).total_seconds() / 60.0

    @staticmethod
    def is_weekday() -> bool:
        return datetime.now().weekday() < 5

    def get_stock_name(self, stock_code: str) -> str:
        """종목명 — KIS 상품마스터(search-stock-info) 권위 조회 우선.

        시세(inquire-price)의 hts_kor_isnm 은 합성 ETF 등 일부 종목에서 비어 코드로 폴백된다
        (예: 256750 KODEX 차이나심천ChiNext 합성 → 시세엔 이름 없음). 상품마스터는 prdt_abrv_name
        으로 정식 약칭을 주므로 이를 단일 권위 소스로 쓴다(LLM 선별/스테일 데이터의 잘못된 이름 교정).
        이름은 불변이라 1회 캐시. 실패 시 시세명, 그래도 없으면 코드."""
        cache = getattr(self, "_stock_name_cache", None)
        if cache is None:
            cache = self._stock_name_cache = {}
        if stock_code in cache:
            return cache[stock_code]
        name = ""
        try:
            data = self._get("/uapi/domestic-stock/v1/quotations/search-stock-info", "CTPF1604R",
                             {"PRDT_TYPE_CD": "300", "PDNO": stock_code})
            out = data.get("output", {}) or {}
            name = (out.get("prdt_abrv_name") or out.get("prdt_name") or "").strip()
        except Exception:
            name = ""
        if not name or name == stock_code:
            try:
                name = self.get_current_price(stock_code).get("name", stock_code)
            except Exception:
                name = stock_code
        name = name or stock_code
        cache[stock_code] = name
        return name

    # ══════════════════════════════════════════════
    # 미국 주식 (해외주식)
    # ══════════════════════════════════════════════
    # 거래소 코드: NASD=나스닥, NYSE=뉴욕, AMEX=아멕스

    # KIS API 거래소 코드 매핑 (시세 조회용)
    _EXCHANGE_MAP_QUOTE = {
        # 미국
        "NASD": "NAS", "NASDAQ": "NAS", "NAS": "NAS",
        "NYSE": "NYS", "NYS": "NYS",
        "AMEX": "AMS", "AMS": "AMS",
        # 일본 — 도쿄증권거래소
        "TYO": "TSE", "JP": "TSE", "TSE": "TSE", "TOKYO": "TSE",
        # 홍콩
        "HK": "HKS", "HKG": "HKS", "HKS": "HKS", "HKEX": "HKS",
        # 중국 본토
        "SHA": "SHS", "SHS": "SHS", "SSE": "SHS", "SHANGHAI": "SHS",
        "SHE": "SZS", "SZS": "SZS", "SZSE": "SZS", "SHENZHEN": "SZS",
        # 베트남
        "HSX": "HSX", "HOSE": "HSX", "HCM": "HSX",
        "HNX": "HNX", "HAN": "HNX",
    }
    # 주문용 (시세와 다름!)
    _EXCHANGE_MAP_ORDER = {
        "NAS": "NASD", "NASD": "NASD", "NASDAQ": "NASD",
        "NYS": "NYSE", "NYSE": "NYSE",
        "AMS": "AMEX", "AMEX": "AMEX",
    }

    @classmethod
    def _to_kis_exchange(cls, exchange: str) -> str:
        """시세 조회용 거래소 코드."""
        return cls._EXCHANGE_MAP_QUOTE.get(exchange.upper(), "NAS")

    @classmethod
    def _to_kis_exchange_order(cls, exchange: str) -> str:
        """주문용 거래소 코드."""
        return cls._EXCHANGE_MAP_ORDER.get(exchange.upper(), "NASD")

    def get_us_current_price(self, ticker: str, exchange: str = "NASD") -> dict:
        """미국 주식 현재가 조회."""
        tr_id = "HHDFS00000300"
        params = {
            "AUTH": "",
            "EXCD": self._to_kis_exchange(exchange),
            "SYMB": ticker,
        }
        data = self._get("/uapi/overseas-price/v1/quotations/price", tr_id, params)
        output = data.get("output", {})

        return {
            "price": float(output.get("last", 0)),
            "change_rate": float(output.get("rate", 0)),
            "volume": int(output.get("tvol", 0)),
            "high": float(output.get("high", 0)),
            "low": float(output.get("low", 0)),
            "open": float(output.get("open", 0)),
            "prev_close": float(output.get("base", 0)),
            "name": output.get("rsym", ticker).replace(f"{exchange}", "").strip(),
            "currency": "USD",
        }

    def get_us_ohlcv(
        self, ticker: str, exchange: str = "NASD", period: str = "D", count: int = 100
    ) -> pd.DataFrame | None:
        """미국 주식 일봉 OHLCV.

        Args:
            ticker: 종목 심볼 (예: "AAPL", "TSLA")
            exchange: NASD, NYSE, AMEX
            period: "D"=일봉, "W"=주봉, "M"=월봉
        """
        tr_id = "HHDFS76240000"
        end_date = datetime.now().strftime("%Y%m%d")

        period_code = {"D": "0", "W": "1", "M": "2"}.get(period, "0")

        params = {
            "AUTH": "",
            "EXCD": self._to_kis_exchange(exchange),
            "SYMB": ticker,
            "GUBN": period_code,
            "BYMD": end_date,
            "MODP": "1",  # 수정주가
        }
        data = self._get(
            "/uapi/overseas-price/v1/quotations/dailyprice", tr_id, params
        )
        output = data.get("output2", [])

        if not output:
            return None

        rows = []
        for item in output:
            try:
                date_str = item.get("xymd", "")
                if not date_str:
                    continue
                rows.append({
                    "date": pd.to_datetime(date_str),
                    "open": float(item.get("open", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "close": float(item.get("clos", 0)),
                    "volume": int(item.get("tvol", 0)),
                })
            except (KeyError, ValueError):
                continue

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df = df[df["close"] > 0]
        df = df.set_index("date").sort_index()
        return df.tail(count)

    def get_us_daily_long(self, ticker: str, exchange: str = "NASD",
                          days: int = 250, period: str = "D") -> pd.DataFrame | None:
        """미국 주식 장기 일봉 — dailyprice(HHDFS76240000, ~100봉/call) BYMD 청크 조회.

        get_us_ohlcv는 1콜 ~100봉이라 1년 백테스트/모멘텀에 부족. BYMD(기준일)를 과거로
        옮겨가며 합쳐 최대 `days`봉을 만든다. get_daily_long(국내)의 해외판.
        """
        tr_id = "HHDFS76240000"
        excd = self._to_kis_exchange(exchange)
        period_code = {"D": "0", "W": "1", "M": "2"}.get(period, "0")
        all_rows: dict = {}
        cursor = datetime.now()
        for _ in range((days // 100) + 2):
            params = {
                "AUTH": "", "EXCD": excd, "SYMB": ticker,
                "GUBN": period_code, "BYMD": cursor.strftime("%Y%m%d"), "MODP": "1",
            }
            try:
                data = self._get(
                    "/uapi/overseas-price/v1/quotations/dailyprice", tr_id, params)
            except Exception:
                break
            output = data.get("output2", []) or []
            got = 0
            oldest = None
            for item in output:
                ds = item.get("xymd", "")
                if not ds or float(item.get("clos", 0) or 0) <= 0:
                    continue
                try:
                    all_rows[ds] = {
                        "date": pd.to_datetime(ds),
                        "open": float(item.get("open", 0)), "high": float(item.get("high", 0)),
                        "low": float(item.get("low", 0)), "close": float(item.get("clos", 0)),
                        "volume": int(item.get("tvol", 0) or 0),
                    }
                    got += 1
                    if oldest is None or ds < oldest:
                        oldest = ds
                except (KeyError, ValueError):
                    continue
            if len(all_rows) >= days or got == 0 or oldest is None:
                break
            cursor = pd.to_datetime(oldest) - timedelta(days=1)
        if not all_rows:
            return None
        df = pd.DataFrame(list(all_rows.values())).set_index("date").sort_index()
        return df.tail(days)

    def get_us_minute_ohlcv(
        self, ticker: str, exchange: str = "NASD", minutes: int = 5,
        count: int = 120,
    ) -> pd.DataFrame | None:
        """미국 주식 분봉 OHLCV (5/1 추가).

        Args:
            ticker: 종목 심볼
            exchange: NASD, NYSE, AMEX
            minutes: 1, 5, 10, 30, 60 (KIS는 분봉 단위 제한)
            count: 조회할 봉 수 (최대 120)
        """
        tr_id = "HHDFS76950200"
        # 분봉은 NMIN(분 단위)을 직접 전달한다(아래 params).

        params = {
            "AUTH": "",
            "EXCD": self._to_kis_exchange(exchange),
            "SYMB": ticker,
            "NMIN": str(minutes),  # 분 단위 (1/5/10 등)
            "PINC": "1",  # 1=정규장, 0=정규장 외
            "NEXT": "",
            "NREC": str(count),
            "FILL": "",
            "KEYB": "",
        }
        try:
            data = self._get(
                "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
                tr_id, params,
            )
        except Exception as e:
            logger.debug("US 분봉 조회 실패 %s: %s", ticker, str(e)[:80])
            return None
        output = data.get("output2", [])
        if not output:
            return None

        rows = []
        for item in output:
            try:
                # KIS 해외분봉: kymd(날짜) + khms(시각)
                ymd = item.get("kymd", "")
                hms = item.get("khms", "")
                if not ymd or not hms:
                    continue
                ts = pd.to_datetime(f"{ymd} {hms}", format="%Y%m%d %H%M%S",
                                    errors="coerce")
                if pd.isna(ts):
                    continue
                rows.append({
                    "date": ts,
                    "open": float(item.get("open", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "close": float(item.get("last", 0)),
                    "volume": int(float(item.get("evol", 0) or 0)),
                })
            except (KeyError, ValueError):
                continue
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df = df[df["close"] > 0]
        df = df.set_index("date").sort_index()
        return df.tail(count)

    def get_us_balance(self) -> dict:
        """해외주식 잔고 조회 (5초 TTL 캐시).

        Returns:
            {
                "cash_usd": 미국 예수금(USD, settled only),
                "display_cash_usd": 한투 앱 일치 (settled + 미정산),
                "sell_pending_usd": T+1 미정산 매도,
                "total_eval_usd": 총평가(USD),
                ...
            }
        """
        cache = getattr(self, "_us_balance_cache", None)
        now_ts = time.time()
        if cache and (now_ts - cache.get("ts", 0) < self._BALANCE_TTL_SEC):
            return cache["data"]
        try:
            result = self._fetch_us_balance_uncached()
        except Exception as e:
            # 서버 500/연결 폭주 시 직전 캐시 폴백 (KR get_balance와 동일,
            if cache and cache.get("data"):
                logger.warning("US 잔고 조회 실패 → 직전 캐시 사용(%.0fs 경과): %s",
                               now_ts - cache.get("ts", 0), str(e)[:120])
                return cache["data"]
            raise
        self._us_balance_cache = {"ts": now_ts, "data": result}
        return result

    def _fetch_us_balance_uncached(self) -> dict:
        tr_id = "VTTS3012R" if self.is_virtual else "TTTS3012R"
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_prod,
            "OVRS_EXCG_CD": "NASD",
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        data = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-balance", tr_id, params
        )

        holdings = []
        for item in data.get("output1", []):
            qty = int(item.get("ovrs_cblc_qty", 0))
            if qty <= 0:
                continue
            avg_price = float(item.get("pchs_avg_pric", 0))
            current = float(item.get("now_pric2", 0))
            profit_rate = float(item.get("evlu_pfls_rt", 0))
            holdings.append({
                "ticker": item.get("ovrs_pdno", ""),
                "name": item.get("ovrs_item_name", ""),
                "exchange": item.get("ovrs_excg_cd", ""),
                "qty": qty,
                "avg_price": avg_price,
                "current_price": current,
                "eval_amount": float(item.get("ovrs_stck_evlu_amt", 0)),
                "profit": float(item.get("frcr_evlu_pfls_amt", 0)),
                "profit_rate": profit_rate,
            })

        summary = data.get("output2", [{}])
        if isinstance(summary, list):
            summary = summary[0] if summary else {}

        # cash_usd: 우선순위
        # 1) inquire-present-balance의 output2[USD].frcr_dncl_amt_2 (실 외화예수금, 결제 전 매도금 포함)
        # 2) inquire-balance output2.frcr_dncl_amt_2 (해외주식 계좌에서 None일 수 있음)
        # 3) inquire-psamount.ord_psbl_frcr_amt (출금가능액 — 결제 전 금액 빠짐, 마지막 폴백)
        cash_usd = 0.0
        sell_pending_usd = 0.0  # USD 미정산 매도 (T+1 결제 대기)
        us_eval_usd = 0.0       # 보유 종목 평가 (USD)
        # 한투 앱과 일치하는 원화환산 값 (output3)
        total_eval_krw = 0
        total_asset_krw = 0
        unsettled_sell_krw = 0  # KR+US 미정산 매도 합계 (원화)
        unsettled_buy_krw = 0   # KR+US 미정산 매수 합계 (원화) — 매도 미정산과 짝
        # 외화 계좌 내 KRW 원화 잔고 (환전 대기 자금) — 100,000원 입금 후 미환전 시 여기 잔존
        us_krw_in_account = 0
        # output2 per-currency breakdown (디버그·검증용)
        us_currency_breakdown: list[dict] = []
        try:
            import requests
            h_pb = {
                "authorization": f"Bearer {self._access_token}",
                "appkey": self.app_key, "appsecret": self.app_secret,
                "content-type": "application/json", "tr_id": "CTRP6504R",
            }
            p_pb = {
                "CANO": self.account_no, "ACNT_PRDT_CD": self.account_prod,
                "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840",
                "TR_MKET_CD": "00", "INQR_DVSN_CD": "00",
            }
            r_pb = requests.get(
                f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance",
                headers=h_pb, params=p_pb, timeout=10,
            ).json()
            for ccy in r_pb.get("output2", []):
                code = ccy.get("crcy_cd", "")
                dncl = float(ccy.get("frcr_dncl_amt_2") or 0)
                sell = float(ccy.get("frcr_sll_amt_smtl") or 0)
                # KRW 환산 (외화 → 원화). KRW 자체는 환산 = 본값
                wcrc = float(ccy.get("wcrc_frcr_dncl_amt_1") or 0)
                us_currency_breakdown.append({
                    "currency": code,
                    "deposit": dncl,
                    "sell_pending": sell,
                    "krw_equiv": wcrc or (dncl if code == "KRW" else 0),
                })
                if code == "USD":
                    cash_usd = dncl
                    sell_pending_usd = sell
                elif code == "KRW":
                    # 외화 계좌의 원화 잔고 (환전 전 자금)
                    us_krw_in_account = int(dncl)
            out3 = r_pb.get("output3", {})
            if isinstance(out3, list):
                out3 = out3[0] if out3 else {}
            # 한투 앱 "총자산" = tot_asst_amt (KR + US 미정산 매도 모두 포함)
            total_asset_krw = int(float(out3.get("tot_asst_amt") or 0))
            total_eval_krw = int(float(out3.get("evlu_amt_smtl_amt") or 0))
            # 미정산 매도 합계 (KR T+2 + US T+1 통합)
            unsettled_sell_krw = int(float(out3.get("ustl_sll_amt_smtl") or 0))
            # 미정산 매수 합계 — 매도 미정산과 짝지어 진짜 자산 영향 판단용
            unsettled_buy_krw = int(float(out3.get("ustl_buy_amt_smtl") or 0))
        except Exception:
            pass

        # us_eval_usd: 보유 종목 평가 합계 (USD). output1에서 직접 합산.
        try:
            us_eval_usd = sum(float(h.get("eval_amount", 0)) for h in holdings)
        except Exception:
            us_eval_usd = 0.0

        if cash_usd <= 0:
            cash_usd = float(summary.get("frcr_dncl_amt_2") or 0)
        if cash_usd <= 0:
            try:
                import requests
                h2 = {
                    "authorization": f"Bearer {self._access_token}",
                    "appkey": self.app_key, "appsecret": self.app_secret,
                    "content-type": "application/json", "tr_id": "TTTS3007R",
                }
                p2 = {
                    "CANO": self.account_no, "ACNT_PRDT_CD": self.account_prod,
                    "OVRS_EXCG_CD": "NASD", "OVRS_ORD_UNPR": "0", "ITEM_CD": "AAPL",
                }
                r2 = requests.get(
                    f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount",
                    headers=h2, params=p2, timeout=10,
                )
                out = r2.json().get("output", {})
                cash_usd = float(out.get("ord_psbl_frcr_amt") or 0)
            except Exception:
                pass

        # 한투 앱 "USD 예수금"과 일치하도록 cash + 미정산 합 노출.
        # cash_usd: 즉시 매수 가능 (settled)
        # display_cash_usd: 한투 앱 표시 = settled + 미정산 매도 (T+1 결제 대기 포함)
        # 매수 게이트는 cash_usd, 표시·총자산 계산은 display_cash_usd 사용.
        display_cash_usd = cash_usd + sell_pending_usd
        return {
            "cash_usd": cash_usd,                    # 매수 가능 (settled)
            "sell_pending_usd": sell_pending_usd,    # USD 미정산 매도 (T+1)
            "display_cash_usd": display_cash_usd,    # 한투 앱과 일치 (settled + 미정산)
            "us_eval_usd": us_eval_usd,              # 보유 평가 (USD) — output1 합산
            # 외화 계좌 내 원화 잔고 — 환전 전 KRW 입금 자금. 100,000원 미국장 입금 후
            # 미환전이면 여기에 잡힘. compute_total_equity가 누락하지 않도록 노출.
            "us_krw_in_account": us_krw_in_account,
            "currency_breakdown": us_currency_breakdown,  # 디버그·검증
            "total_eval_usd": float(summary.get("tot_evlu_pfls_amt") or 0),
            "total_profit_usd": float(summary.get("ovrs_tot_pfls") or 0),
            "total_profit_rate": float(summary.get("tot_pftrt") or 0),
            "holdings": holdings,
            # 한투 앱 표시와 동일한 원화환산 (inquire-present-balance.output3)
            "total_eval_krw": total_eval_krw,
            "total_asset_krw": total_asset_krw,     # 한투 "총자산" (미정산 매도 포함)
            "unsettled_sell_krw": unsettled_sell_krw,  # KR T+2 + US T+1 미정산 매도 합계
            "unsettled_buy_krw": unsettled_buy_krw,    # KR T+2 + US T+1 미정산 매수 합계 (짝)
        }

    def get_usd_krw_rate(self) -> float:
        """USD/KRW 환율 (KIS 매수가능금액 조회의 exrt 필드 사용, 10분 캐시).

        실패 시 1350 반환.
        """
        import time as _t
        cache = getattr(self, "_fx_cache", None)
        now = _t.time()
        if cache and now - cache["ts"] < 600:
            return cache["rate"]
        try:
            import requests
            self._ensure_token()
            headers = {
                "authorization": f"Bearer {self._access_token}",
                "appkey": self.app_key, "appsecret": self.app_secret,
                "content-type": "application/json", "tr_id": "TTTS3007R",
            }
            params = {
                "CANO": self.account_no, "ACNT_PRDT_CD": self.account_prod,
                "OVRS_EXCG_CD": "NASD", "OVRS_ORD_UNPR": "0", "ITEM_CD": "AAPL",
            }
            r = requests.get(
                f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount",
                headers=headers, params=params, timeout=10,
            )
            output = r.json().get("output", {})
            rate = float(output.get("exrt", 0) or 0)
            if rate <= 0:
                rate = 1350.0
            self._fx_cache = {"ts": now, "rate": rate}
            return rate
        except Exception:
            return cache["rate"] if cache else 1350.0

    def sell_us_market(self, ticker: str, qty: int, exchange: str = "NASD") -> dict:
        """미국 주식 시장가 매도."""
        return self._us_order(ticker, "sell", qty, price=0, exchange=exchange)

    def buy_us_limit(self, ticker: str, qty: int, price: float, exchange: str = "NASD") -> dict:
        """미국 주식 지정가 매수."""
        return self._us_order(ticker, "buy", qty, price=price, exchange=exchange)

    def sell_us_limit(self, ticker: str, qty: int, price: float, exchange: str = "NASD") -> dict:
        """미국 주식 지정가 매도."""
        return self._us_order(ticker, "sell", qty, price=price, exchange=exchange)

    def _us_order(self, ticker: str, side: str, qty: int, price: float, exchange: str = "NASD") -> dict:
        """미국 주식 주문."""
        if side == "buy":
            tr_id = "VTTT1002U" if self.is_virtual else "TTTT1002U"
        else:
            tr_id = "VTTT1001U" if self.is_virtual else "TTTT1006U"

        # 시장가: ORD_DVSN="00" + 가격 0
        orig_price = price
        market_price = 0.0
        order_type = "00"  # 지정가
        if price == 0:
            order_type = "00"
            # 미국 시장가는 현재가로 지정가 주문
            info = self.get_us_current_price(ticker, exchange)
            market_price = float(info["price"])
            price = market_price * (1.01 if side == "buy" else 0.99)  # 슬리피지 허용
        elif price and price > 0:
            try:
                market_price = float(self.get_us_current_price(ticker, exchange)["price"])
            except Exception:
                market_price = 0.0

        # ── 주문 관문 안전 검증 (변조/조작 방어) — fail-closed ──
        # 시장가(orig_price=0)는 가격검증 제외, 지정가만 밴드 검증. 수량·워시·코드는 항상.
        _prev = self._last_order_ts.get(ticker)
        _last_opp = _prev[1] if (_prev and _prev[0] != side) else 0.0
        _ok, _why = self._order_safety.validate(
            side=side, code=ticker, qty=qty,
            price=(orig_price if orig_price and orig_price > 0 else 0),
            order_type=("00" if orig_price and orig_price > 0 else "01"),
            market_price=market_price, last_opposite_ts=_last_opp)
        if not _ok:
            logger.critical("주문 안전 차단 (US %s %s %s주): %s", side, ticker, qty, _why)
            return {"success": False, "message": f"안전 검증 차단: {_why}",
                    "order_no": "", "blocked": True}
        self._last_order_ts[ticker] = (side, time.time())

        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_prod,
            "OVRS_EXCG_CD": self._to_kis_exchange_order(exchange),
            "PDNO": ticker,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": order_type,
        }

        logger.info("%s 주문(US): %s %d주 @ $%.2f (%s)",
                     "매수" if side == "buy" else "매도", ticker, qty, price, exchange)

        data = self._post("/uapi/overseas-stock/v1/trading/order", tr_id, body)

        result = {
            "success": data.get("rt_cd") == "0",
            "message": data.get("msg1", ""),
            "order_no": data.get("output", {}).get("ODNO", ""),
        }
        logger.info("주문 결과(US): %s", result)
        if result["success"]:
            self.invalidate_balance_cache()
        return result

    # ── 미국 시장 시간 (KST 기준) ──

    @staticmethod
    def us_market_phase() -> str:
        """미국 장 상태 (KST 기준).

        미국 정규장: ET 09:30~16:00
          → 서머타임(3~11월): KST 22:30~05:00 (+1일)
          → 겨울(11~3월):     KST 23:30~06:00 (+1일)

        Returns:
            "pre_market", "open", "post_market", "closed"
        """
        now = datetime.now()
        if now.weekday() >= 5:
            # 토요일 새벽(금요일 장)은 열릴 수 있음
            if now.weekday() == 5 and now.hour < 7:
                pass  # 토요일 새벽 — 아래 로직으로 계속
            else:
                return "closed"

        t = now.hour * 100 + now.minute

        # 서머타임 판단 (3월 둘째 일요일 ~ 11월 첫째 일요일)
        is_dst = _is_us_dst(now)

        # pre_market은 개장 1시간 전부터 — 장전 리포트(LLM+웹검색 수 분)와 종목 재선별이
        # 개장 전에 끝나야 개장 매수 우선순위가 실제 개장에 맞춰 준비된다.
        # (기존 30분 창은 분석이 개장을 넘겨 "장전" 분석이 장중에 도착하던 원인)
        if is_dst:
            # 서머타임: KST 22:30~05:00
            pre_open = 2130
            market_open = 2230
            market_close = 500  # 다음날
            post_close = 600
        else:
            # 겨울: KST 23:30~06:00
            pre_open = 2230
            market_open = 2330
            market_close = 600  # 다음날
            post_close = 700

        # 자정을 넘기는 시간대 처리
        if market_open > market_close:
            # 밤~새벽 구간
            if t >= pre_open:
                return "pre_market" if t < market_open else "open"
            #: 월요일 새벽(KST)은 ET 일요일 → 미국 휴장. 전 세션은 금요일이
            # 마지막이고 일요일 밤엔 세션이 시작되지 않으므로 야간크로싱 'open' 오판 방지.
            # (월요일 정규장은 같은 날 밤 market_open=23:30/22:30부터 위 분기로 처리됨.)
            if now.weekday() == 0:
                return "closed"
            elif t < market_close:
                return "open"
            elif t < post_close:
                return "post_market"
            return "closed"
        else:
            if pre_open <= t < market_open:
                return "pre_market"
            if market_open <= t < market_close:
                return "open"
            if market_close <= t < post_close:
                return "post_market"
            return "closed"

    @staticmethod
    def is_us_market_open() -> bool:
        """미국 정규장 시간인지 확인 (KST 기준)."""
        return KISClient.us_market_phase() == "open"

    @staticmethod
    def us_minutes_to_close() -> "float | None":
        """미국 정규장 마감까지 남은 분(KST). 정규장(open)이 아니면 None.

        마감 KST 05:00(서머)/06:00(겨울). 저녁 개장(22:30/23:30) 세션은 마감이 익일 새벽이라
        날짜를 +1 보정한다. 인버스 EOD 수익 락인의 '마감 임박' 판정용.
        """
        if KISClient.us_market_phase() != "open":
            return None
        now = datetime.now()
        close_h = 5 if _is_us_dst(now) else 6
        close = now.replace(hour=close_h, minute=0, second=0, microsecond=0)
        if now.hour >= 12:   # 저녁 세션 — 마감은 익일 새벽
            close += timedelta(days=1)
        return (close - now).total_seconds() / 60.0


def _is_us_dst(dt: datetime) -> bool:
    """미국 서머타임 여부 (3월 둘째 일요일 ~ 11월 첫째 일요일)."""
    year = dt.year
    # 3월 둘째 일요일
    march_first = datetime(year, 3, 1)
    march_second_sunday = march_first + timedelta(days=(6 - march_first.weekday() + 7) % 7 + 7)
    if march_second_sunday.day > 14:
        march_second_sunday -= timedelta(days=7)
    # 11월 첫째 일요일
    nov_first = datetime(year, 11, 1)
    nov_first_sunday = nov_first + timedelta(days=(6 - nov_first.weekday()) % 7)

    dst_start = march_second_sunday.replace(hour=2)
    dst_end = nov_first_sunday.replace(hour=2)

    return dst_start <= dt < dst_end
