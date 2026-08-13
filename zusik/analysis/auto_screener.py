from __future__ import annotations
"""자동 스크리닝 — 후보 풀에서 MC 통계 + 추세 필터로 상위 종목 선정.

매일 1회 (post_market 시점) 실행:
  1. 후보 풀 100+ 종목 OHLCV fetch (병렬)
  2. 각 종목에 대해 Vortex MC 1만 path × 30일 시뮬
  3. P(profit>0), VaR(95%), 추세 종합 점수 산출
  4. 상위 N (KR 5종, US 5종) 자동 선정 → watch list 갱신

Vortex 8x 가속이 100+ 종목 일괄 평가에 핵심 — 100종 × MC 80ms = 8초.
numpy로는 100종 × 700ms = 70초.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)


# 후보 풀 (시드 10만원 호환 + 시총 큰 우량주 + 다양한 섹터 ETF)
# KIS API에서 OHLCV 조회 가능한 종목.
# Vortex 가속(8x) 활용 → 100+ 종목 일일 평가 가능.
KR_CANDIDATE_POOL = [
    # ══ 인덱스 ETF (시장 추종) ══
    ("102110", "TIGER 200"),
    ("069500", "KODEX 200"),
    ("232080", "TIGER 코스닥150"),
    ("251340", "KODEX 코스닥150"),
    ("310970", "TIGER 코스피"),
    ("277630", "TIGER 코스닥150 IT"),
    # ══ 미국 인덱스 ETF (KR-listed, 시드 호환) ══
    ("360750", "TIGER 미국S&P500"),
    ("133690", "TIGER 미국나스닥100"),
    ("381180", "TIGER 미국필라델피아반도체"),
    ("394670", "TIGER 미국테크TOP10 INDXX"),
    ("411420", "TIGER 미국S&P500커버드콜"),
    ("133690", "TIGER 미국나스닥100"),
    ("381170", "TIGER 미국S&P500선물(H)"),
    ("251350", "KODEX 선진국MSCI World"),
    # ══ 섹터/테마 ETF ══
    ("091160", "KODEX 반도체"),
    ("091170", "KODEX 은행"),
    ("305720", "KODEX 2차전지산업"),
    ("305080", "TIGER 200 IT"),
    ("266370", "KODEX 200 IT TR"),
    ("139260", "TIGER 200 IT"),
    ("117460", "KODEX 에너지화학"),
    ("139220", "TIGER 200 산업재"),
    ("139250", "TIGER 200 중공업"),
    ("261060", "KODEX 게임산업"),
    ("228820", "TIGER 의료기기"),
    ("228810", "TIGER 화장품"),
    ("325020", "TIGER 미디어컨텐츠"),
    ("228800", "TIGER 여행레저"),
    ("228790", "TIGER 화학"),
    ("139230", "TIGER 200 생활소비재"),
    ("228830", "TIGER 헬스케어"),
    ("139270", "TIGER 200 금융"),
    ("228780", "TIGER 200 철강소재"),
    ("117680", "KODEX 철강"),
    ("277630", "TIGER 코스닥150 IT"),
    ("364980", "TIGER KRX BBIG K-뉴딜"),
    ("364990", "TIGER KRX 2차전지 K-뉴딜"),
    ("305540", "TIGER 2차전지테마"),
    # ══ 인버스/헷지 ETF ══
    ("114800", "KODEX 인버스"),
    ("252670", "KODEX 200선물인버스2X"),
    ("409820", "KODEX 미국나스닥100인버스(H)"),
    ("251340", "KODEX 코스닥150선물인버스"),
    ("252710", "TIGER 200선물인버스2X"),
    # ══ 원자재/대체자산 ETF ══
    ("261240", "KODEX WTI원유선물(H)"),
    ("144600", "KODEX 골드선물(H)"),
    ("132030", "KODEX 골드선물H"),
    ("261220", "KODEX WTI원유선물인버스(H)"),
    ("136340", "TIGER 미국채10년선물"),
    ("267440", "KODEX 미국S&P500선물(H)"),
    # ══ 저가 우량주 (≤ 5만원) ──
    # 금융
    ("316140", "우리금융지주"),
    ("055550", "신한지주"),
    ("105560", "KB금융"),
    ("086790", "하나금융지주"),
    ("024110", "기업은행"),
    ("138040", "메리츠금융지주"),
    ("032830", "삼성생명"),
    ("088980", "맥쿼리인프라"),
    ("000810", "삼성화재"),
    ("001450", "현대해상"),
    # 통신/유틸리티
    ("015760", "한국전력"),
    ("034730", "SK"),
    ("030200", "KT"),
    ("017670", "SK텔레콤"),
    ("033780", "KT&G"),
    ("018670", "SK가스"),
    ("036460", "한국가스공사"),
    # 자동차/조선/방산
    ("000270", "기아"),
    ("003490", "대한항공"),
    ("000720", "현대건설"),
    ("010620", "현대미포조선"),
    ("011200", "HMM"),
    ("009540", "HD한국조선해양"),
    ("064350", "현대로템"),
    ("047810", "한국항공우주"),
    ("010140", "삼성중공업"),
    ("034020", "두산에너빌리티"),
    # 화학/소재
    ("009830", "한화솔루션"),
    ("011170", "롯데케미칼"),
    ("003670", "포스코퓨처엠"),
    ("161390", "한국타이어앤테크놀로지"),
    ("267260", "HD현대일렉트릭"),
    ("004020", "현대제철"),
    ("001740", "SK네트웍스"),
    ("004990", "롯데지주"),
    ("004000", "롯데정밀화학"),
    ("000150", "두산"),
    # 반도체/IT
    ("042700", "한미반도체"),
    ("034220", "LG디스플레이"),
    ("011070", "LG이노텍"),
    ("018260", "삼성에스디에스"),
    ("066570", "LG전자"),
    ("066970", "엘앤에프"),
    ("251270", "넷마블"),
    ("078930", "GS"),
    ("259960", "크래프톤"),
    ("293490", "카카오게임즈"),
    # 바이오/헬스케어
    ("003850", "보령"),
    ("145020", "휴젤"),
    ("214150", "클래시스"),
    ("196170", "알테오젠"),
    # 소비재/유통
    ("028260", "삼성물산"),
    ("057050", "현대홈쇼핑"),
    ("139480", "이마트"),
    ("004170", "신세계"),
    ("282330", "BGF리테일"),
    # 코스닥 우량주
    ("058470", "리노공업"),
    ("196170", "알테오젠"),
    ("328130", "루닛"),
    ("277810", "레인보우로보틱스"),
    ("357780", "솔브레인"),
    ("042700", "한미반도체"),
    ("095660", "네오위즈"),
    # ══ 메가캡 (1주 매수 가능) ══
    ("005930", "삼성전자"),
    ("005935", "삼성전자우"),
    # ══ 추가 KR 우량주 (시드 호환) ══
    ("035720", "카카오"),
    ("066570", "LG전자"),
    ("011070", "LG이노텍"),
    ("018260", "삼성에스디에스"),
    ("251270", "넷마블"),
    ("078930", "GS"),
    ("259960", "크래프톤"),
    ("293490", "카카오게임즈"),
    ("000810", "삼성화재"),
    ("001450", "현대해상"),
    ("018670", "SK가스"),
    ("036460", "한국가스공사"),
    ("034020", "두산에너빌리티"),
    ("004000", "롯데정밀화학"),
    ("000150", "두산"),
    ("066970", "엘앤에프"),
    ("145020", "휴젤"),
    ("214150", "클래시스"),
    ("057050", "현대홈쇼핑"),
    ("139480", "이마트"),
    ("004170", "신세계"),
    ("282330", "BGF리테일"),
    ("058470", "리노공업"),
    ("328130", "루닛"),
    ("277810", "레인보우로보틱스"),
    ("357780", "솔브레인"),
    ("095660", "네오위즈"),
    ("091990", "셀트리온헬스케어"),
    ("008770", "호텔신라"),
    ("035250", "강원랜드"),
    ("026960", "동서"),
    ("097950", "CJ제일제당"),
    ("000080", "하이트진로"),
    ("271560", "오리온"),
    ("271940", "일진하이솔루스"),
    ("178320", "케이엠더블유"),
    ("060280", "큐렉소"),
    ("011780", "금호석유"),
    ("016360", "삼성증권"),
    ("078930", "GS"),
    ("097520", "엠씨넥스"),
    ("014830", "유니드"),
    ("034950", "한국기업평가"),
    ("030000", "제일기획"),
    ("005180", "빙그레"),
    ("002790", "아모레G"),
    ("090430", "아모레퍼시픽"),
    ("128940", "한미약품"),
    ("009150", "삼성전기"),
    ("298050", "효성첨단소재"),
    ("298040", "효성중공업"),
    ("267250", "HD현대"),
    ("014820", "동원시스템즈"),
    ("000100", "유한양행"),
    ("003090", "대웅"),
    ("009240", "한샘"),
    ("241560", "두산밥캣"),
    ("042660", "한화오션"),
    ("003410", "쌍용C&E"),
    ("011790", "SKC"),
    ("103140", "풍산"),
    # ══ 추가 KR ETF ══
    ("379800", "KODEX 미국S&P500"),
    ("379780", "KODEX 미국나스닥100"),
    ("365040", "KODEX 미국FANG플러스(H)"),
    ("446720", "KODEX 미국빅테크 TOP10"),
    ("411430", "KODEX 미국S&P500선물"),
    ("309230", "KOSEF 200TR"),
    ("091230", "TIGER 헬스케어"),
    ("252720", "TIGER 200 화학"),
    ("273130", "KODEX 종합채권(AA-이상)액티브"),
    ("273140", "KODEX 단기채권PLUS"),
    ("153130", "KODEX 단기채권"),
    ("213610", "TIGER 단기통안채"),
    ("256750", "TIGER 미국MSCI리츠"),
    ("267770", "TIGER 200 헬스케어"),
    ("104530", "TIGER 200 에너지화학"),
    ("139310", "TIGER 200 건설"),
    ("139220", "TIGER 200 산업재"),
    ("139250", "TIGER 200 중공업"),
    ("117460", "KODEX 에너지화학"),
    ("139260", "TIGER 200 IT"),
    ("169950", "KODEX China A50"),
    ("174360", "KODEX 일본TOPIX100"),
    ("182490", "TIGER 일본TOPIX(합성 H)"),
    ("332620", "TIGER 차이나CSI300"),
    ("371460", "TIGER 차이나전기차SOLACTIVE"),
    ("371870", "TIGER 차이나항셍테크"),
    ("319640", "TIGER 골드선물(H)"),
    ("328370", "TIGER 미국나스닥바이오"),
    ("261110", "TIGER 200 코로나19"),
    ("251590", "TIGER 200선물레버리지"),
    # ══ KOSPI 200 추가 우량주 ══
    ("000020", "동화약품"),
    ("000040", "KR모터스"),
    ("000050", "경방"),
    ("000070", "삼양홀딩스"),
    ("000120", "CJ대한통운"),
    ("000140", "하이트진로홀딩스"),
    ("000210", "DL"),
    ("000240", "한국앤컴퍼니"),
    ("000300", "대유플러스"),
    ("000370", "한화손해보험"),
    ("000390", "삼화페인트공업"),
    ("000400", "롯데손해보험"),
    ("000430", "대원강업"),
    ("000480", "조선내화"),
    ("000490", "대동"),
    ("000500", "가온전선"),
    ("000540", "흥국화재"),
    ("000545", "흥국화재우"),
    ("000640", "동아쏘시오홀딩스"),
    ("000660", "SK하이닉스"),  # 비싼 종목, 시드 부족 자동 차단
    ("000670", "영풍"),
    ("000700", "유수홀딩스"),
    ("000760", "이화산업"),
    ("000810", "삼성화재"),
    ("000880", "한화"),
    ("000890", "보해양조"),
    ("000910", "유니온"),
    ("000950", "전방"),
    ("000970", "한국주철관"),
    ("000990", "DB하이텍"),
    ("001020", "페이퍼코리아"),
    ("001060", "JW중외제약"),
    ("001120", "LX인터내셔널"),
    ("001130", "대한제분"),
    ("001230", "동국홀딩스"),
    ("001250", "GS글로벌"),
    ("001270", "부국증권"),
    ("001290", "상상인증권"),
    ("001340", "백광산업"),
    ("001360", "삼양사"),
    ("001380", "SG글로벌"),
    ("001440", "대한전선"),
    ("001460", "BYC"),
    ("001470", "삼부토건"),
    ("001500", "현대차증권"),
    ("001520", "동양"),
    ("001530", "DI동일"),
    ("001550", "조비"),
    ("001620", "케이비아이동국실업"),
    ("001630", "종근당홀딩스"),
    ("001680", "대상"),
    ("001750", "한양증권"),
    ("001780", "알루코"),
    ("001790", "대한제당"),
    ("001800", "오리온홀딩스"),
    ("001820", "삼화콘덴서"),
    ("001880", "DL건설"),
    ("002020", "코오롱"),
    ("002030", "아세아"),
    ("002100", "경농"),
    ("002150", "도화엔지니어링"),
    ("002170", "삼양통상"),
    ("002240", "고려제강"),
    ("002270", "롯데푸드"),
    ("002310", "아세아제지"),
    ("002320", "한진"),
    ("002350", "넥센타이어"),
    ("002360", "SH에너지화학"),
    ("002380", "KCC"),
    ("002390", "한일하이닉스"),
    ("002400", "TCC스틸"),
    ("002410", "범양건영"),
    ("002450", "삼익THK"),
    ("002460", "화성산업"),
    ("002600", "조흥"),
    ("002620", "제이준코스메틱"),
    ("002630", "오리엔트바이오"),
    ("002700", "신일전자"),
    ("002710", "TCC스틸"),
    ("002720", "국제약품"),
    ("002760", "보락"),
    ("002780", "진흥기업"),
    ("002870", "신풍제지"),
    ("002900", "동양고속"),
    ("002920", "유성기업"),
    ("002990", "금호건설"),
    ("003000", "부광약품"),
    ("003030", "세아제강지주"),
    ("003060", "보령"),
    ("003070", "코오롱글로벌"),
    ("003080", "성보화학"),
    ("003120", "일성건설"),
    ("003160", "디아이"),
    ("003200", "일신방직"),
    ("003220", "대원제약"),
    ("003230", "삼양식품"),
    ("003240", "태광산업"),
    ("003300", "한일홀딩스"),
    ("003410", "쌍용C&E"),
    ("003460", "유화증권"),
    ("003470", "유안타증권"),
    ("003480", "한진중공업홀딩스"),
    ("003520", "영진약품"),
    ("003530", "한화투자증권"),
    ("003540", "대신증권"),
    ("003550", "LG"),  # 비싼 종목, 자동 차단
    ("003570", "에스엠"),
    ("003580", "넥스트사이언스"),
    ("003620", "쌍용차"),
    ("003650", "미창석유"),
    ("003680", "한성기업"),
    ("003690", "코리안리"),
    ("003720", "삼영화학공업"),
    ("003800", "에이스침대"),
    ("003960", "사조대림"),
]

_KR_RAW = KR_CANDIDATE_POOL  # 중복 dedup
KR_CANDIDATE_POOL = []
_seen_kr = set()
for _c, _n in _KR_RAW:
    if _c not in _seen_kr:
        _seen_kr.add(_c)
        KR_CANDIDATE_POOL.append((_c, _n))


_US_RAW = [
    # ══ 저가 메가캡 (≤ $60) ══
    # 통신/유틸리티
    ("T", "AT&T", "NYSE"), ("VZ", "Verizon", "NYSE"), ("CMCSA", "Comcast", "NASD"),
    ("PCG", "PG&E", "NYSE"), ("ED", "Consolidated Edison", "NYSE"),
    # 자동차/물류
    ("F", "Ford", "NYSE"), ("CCL", "Carnival", "NYSE"), ("AAL", "American Airlines", "NASD"),
    ("UAL", "United Airlines", "NASD"), ("DAL", "Delta", "NYSE"), ("LUV", "Southwest", "NYSE"),
    ("UBER", "Uber", "NYSE"), ("LYFT", "Lyft", "NASD"),
    # 금융
    ("BAC", "Bank of America", "NYSE"), ("WFC", "Wells Fargo", "NYSE"),
    ("HBAN", "Huntington", "NASD"), ("CFG", "Citizens Financial", "NYSE"),
    ("RF", "Regions Financial", "NYSE"), ("KEY", "KeyCorp", "NYSE"),
    ("USB", "U.S. Bancorp", "NYSE"), ("MET", "MetLife", "NYSE"),
    ("PRU", "Prudential", "NYSE"), ("AIG", "AIG", "NYSE"),
    ("ALLY", "Ally Financial", "NYSE"), ("DFS", "Discover", "NYSE"),
    ("SOFI", "SoFi", "NASD"), ("NU", "Nubank", "NYSE"),
    # 헬스케어
    ("PFE", "Pfizer", "NYSE"), ("BMY", "Bristol-Myers", "NYSE"),
    ("MRK", "Merck", "NYSE"), ("GILD", "Gilead", "NASD"),
    ("VTRS", "Viatris", "NASD"), ("WBA", "Walgreens", "NASD"),
    ("CVS", "CVS Health", "NYSE"), ("CI", "Cigna", "NYSE"),
    ("HUM", "Humana", "NYSE"), ("BIIB", "Biogen", "NASD"),
    # 소비재
    ("KO", "Coca-Cola", "NYSE"), ("MO", "Altria", "NYSE"),
    ("KR", "Kroger", "NYSE"), ("KMB", "Kimberly-Clark", "NYSE"),
    ("HSY", "Hershey", "NYSE"), ("MDLZ", "Mondelez", "NASD"),
    ("CLX", "Clorox", "NYSE"), ("CHD", "Church & Dwight", "NYSE"),
    ("KHC", "Kraft Heinz", "NASD"), ("STZ", "Constellation Brands", "NYSE"),
    # 에너지/원자재
    ("KMI", "Kinder Morgan", "NYSE"), ("ET", "Energy Transfer", "NYSE"),
    ("WMB", "Williams Companies", "NYSE"), ("OXY", "Occidental", "NYSE"),
    ("SLB", "Schlumberger", "NYSE"), ("HAL", "Halliburton", "NYSE"),
    ("FCX", "Freeport-McMoRan", "NYSE"), ("NEM", "Newmont", "NYSE"),
    ("X", "U.S. Steel", "NYSE"), ("CLF", "Cleveland-Cliffs", "NYSE"),
    ("VALE", "Vale", "NYSE"), ("RIO", "Rio Tinto", "NYSE"),
    ("BTU", "Peabody Energy", "NYSE"), ("AA", "Alcoa", "NYSE"),
    # 기술/반도체 (저가)
    ("CSCO", "Cisco", "NASD"), ("INTC", "Intel", "NASD"),
    ("AMD", "AMD", "NASD"), ("ON", "ON Semiconductor", "NASD"),
    ("MU", "Micron", "NASD"), ("HPQ", "HP Inc", "NYSE"),
    ("DELL", "Dell Technologies", "NYSE"), ("NTAP", "NetApp", "NASD"),
    ("WDC", "Western Digital", "NASD"),
    # 기타 가치주
    ("DOW", "Dow Inc", "NYSE"), ("TPR", "Tapestry", "NYSE"),
    ("ABEV", "Ambev", "NYSE"), ("ITUB", "Itau Unibanco", "NYSE"),
    ("BABA", "Alibaba", "NYSE"), ("JD", "JD.com", "NASD"),
    ("PDD", "PDD Holdings", "NASD"), ("BIDU", "Baidu", "NASD"),

    # ══ 성장주/EV (≤ $50) ══
    ("PLTR", "Palantir", "NYSE"), ("RIVN", "Rivian", "NASD"),
    ("LCID", "Lucid", "NASD"), ("NIO", "NIO", "NYSE"),
    ("XPEV", "XPeng", "NYSE"), ("LI", "Li Auto", "NASD"),
    ("GRAB", "Grab Holdings", "NASD"), ("DIDI", "DiDi", "NYSE"),
    ("SNAP", "Snap", "NYSE"), ("PINS", "Pinterest", "NYSE"),
    ("RBLX", "Roblox", "NYSE"), ("MTCH", "Match Group", "NASD"),
    ("ZM", "Zoom", "NASD"), ("DOCU", "DocuSign", "NASD"),
    ("HOOD", "Robinhood", "NASD"), ("COIN", "Coinbase", "NASD"),
    ("MARA", "Marathon Digital", "NASD"), ("RIOT", "Riot Platforms", "NASD"),
    ("CLSK", "CleanSpark", "NASD"), ("BITF", "Bitfarms", "NASD"),

    # ══ 섹터 ETF (전 섹터 11개) ══
    ("XLF", "Financial SPDR", "AMEX"), ("XLE", "Energy SPDR", "AMEX"),
    ("XLV", "Health Care SPDR", "AMEX"), ("XLK", "Technology SPDR", "AMEX"),
    ("XLP", "Consumer Staples SPDR", "AMEX"), ("XLU", "Utilities SPDR", "AMEX"),
    ("XLI", "Industrial SPDR", "AMEX"), ("XLY", "Consumer Discretionary SPDR", "AMEX"),
    ("XLB", "Materials SPDR", "AMEX"), ("XLRE", "Real Estate SPDR", "AMEX"),
    ("XLC", "Communication SPDR", "AMEX"),
    # 반도체/테크 ETF
    ("SOXX", "iShares Semiconductor", "NASD"), ("SMH", "VanEck Semiconductor", "NASD"),
    ("ARKK", "ARK Innovation", "AMEX"), ("ARKG", "ARK Genomic", "AMEX"),
    ("ARKW", "ARK Next Internet", "AMEX"), ("ARKQ", "ARK Autonomous", "AMEX"),
    # 광역 ETF
    ("VTI", "Vanguard Total Market", "AMEX"), ("DIA", "SPDR Dow", "AMEX"),
    ("IWM", "iShares Russell 2000", "AMEX"), ("MDY", "SPDR S&P 400", "AMEX"),
    # 글로벌 ETF
    ("EEM", "iShares MSCI EM", "AMEX"), ("EWY", "iShares MSCI Korea", "NYSE"),
    ("EWJ", "iShares MSCI Japan", "NYSE"), ("FXI", "iShares China Large-Cap", "NYSE"),
    ("MCHI", "iShares MSCI China", "NASD"), ("INDA", "iShares MSCI India", "AMEX"),
    ("VEA", "Vanguard FTSE Developed", "AMEX"), ("VWO", "Vanguard FTSE EM", "AMEX"),
    ("EWG", "iShares MSCI Germany", "NYSE"), ("EWU", "iShares MSCI UK", "NYSE"),
    # 채권 ETF
    ("HYG", "iShares HY Bond", "AMEX"), ("LQD", "iShares IG Bond", "AMEX"),
    ("TLT", "iShares 20+ Treasury", "NASD"), ("IEF", "iShares 7-10Y Treasury", "NASD"),
    ("SHY", "iShares 1-3Y Treasury", "NASD"), ("AGG", "iShares Aggregate Bond", "AMEX"),
    # 원자재 ETF
    ("GLD", "SPDR Gold", "AMEX"), ("IAU", "iShares Gold", "AMEX"),
    ("SLV", "iShares Silver", "AMEX"), ("USO", "US Oil Fund", "AMEX"),
    ("UNG", "US Natural Gas", "AMEX"), ("CORN", "Teucrium Corn", "AMEX"),
    ("WEAT", "Teucrium Wheat", "AMEX"), ("DBA", "Invesco Agriculture", "AMEX"),
    # REIT ETF
    ("VNQ", "Vanguard Real Estate", "AMEX"), ("IYR", "iShares US Real Estate", "AMEX"),
    # 변동성/안전자산
    ("VIXY", "ProShares VIX Short-Term", "AMEX"), ("UVXY", "ProShares Ultra VIX", "AMEX"),

    # ══ 인버스 ETF (헷지) ══
    ("SQQQ", "ProShares UltraPro Short QQQ", "NASD"),
    ("SH", "ProShares Short S&P 500", "AMEX"),
    ("SDS", "ProShares UltraShort S&P500", "AMEX"),
    ("PSQ", "ProShares Short QQQ", "NASD"),
    ("DOG", "ProShares Short Dow30", "AMEX"),
    ("RWM", "ProShares Short Russell 2000", "AMEX"),
    ("TZA", "Direxion Daily Small Cap Bear 3X", "AMEX"),
    ("SPXU", "ProShares UltraPro Short S&P500", "AMEX"),

    # ══ 테마 ETF (ESG/클린에너지/기술) ══
    ("ICLN", "iShares Clean Energy", "NASD"),
    ("PBW", "Invesco WilderHill Clean", "AMEX"),
    ("TAN", "Invesco Solar", "AMEX"),
    ("FAN", "First Trust Wind Energy", "AMEX"),
    ("LIT", "Global X Lithium", "AMEX"),
    ("URA", "Global X Uranium", "AMEX"),
    ("URNM", "Sprott Uranium Miners", "AMEX"),
    ("KWEB", "KraneShares CSI China Internet", "AMEX"),
    ("KOMP", "SPDR S&P Kensho New Economies", "AMEX"),
    ("BOTZ", "Global X Robotics", "NASD"),
    ("ROBO", "Robo Global Robotics", "NYSE"),
    ("CIBR", "First Trust Cybersecurity", "NASD"),
    ("HACK", "ETFMG Cyber Security", "NYSE"),
    ("PAVE", "Global X Infrastructure", "AMEX"),
    ("XOP", "SPDR S&P Oil & Gas", "AMEX"),
    ("OIH", "VanEck Oil Services", "NYSE"),
    ("KRE", "SPDR S&P Regional Banking", "AMEX"),
    ("KBE", "SPDR S&P Bank", "AMEX"),
    ("IBB", "iShares Biotechnology", "NASD"),
    ("XBI", "SPDR S&P Biotech", "AMEX"),
    ("ITB", "iShares US Home Construction", "AMEX"),
    ("XHB", "SPDR S&P Homebuilders", "AMEX"),
    ("JETS", "U.S. Global Jets", "AMEX"),
    ("GDX", "VanEck Gold Miners", "AMEX"),
    ("GDXJ", "VanEck Junior Gold Miners", "AMEX"),
    ("REMX", "VanEck Rare Earth", "AMEX"),
    ("PICK", "iShares MSCI Global Metals", "NASD"),
    ("COPX", "Global X Copper Miners", "AMEX"),
    ("SLX", "VanEck Steel", "NYSE"),

    # ══ 팩터/스타일 ETF ══
    ("VTV", "Vanguard Value", "AMEX"),
    ("VUG", "Vanguard Growth", "AMEX"),
    ("VYM", "Vanguard High Dividend", "AMEX"),
    ("VIG", "Vanguard Dividend Appreciation", "AMEX"),
    ("SCHD", "Schwab US Dividend Equity", "AMEX"),
    ("DVY", "iShares Select Dividend", "AMEX"),
    ("HDV", "iShares Core High Dividend", "AMEX"),
    ("MOAT", "VanEck Morningstar Wide Moat", "AMEX"),
    ("QUAL", "iShares MSCI USA Quality", "AMEX"),
    ("MTUM", "iShares MSCI USA Momentum", "AMEX"),
    ("VLUE", "iShares MSCI USA Value", "AMEX"),
    ("USMV", "iShares MSCI USA Min Vol", "AMEX"),
    ("SPLV", "Invesco S&P 500 Low Volatility", "AMEX"),
    ("PFF", "iShares Preferred Securities", "NASD"),
    ("SPHD", "Invesco S&P 500 High Div Low Vol", "AMEX"),
    ("NOBL", "ProShares S&P 500 Dividend Aristocrats", "AMEX"),

    # ══ 통화/단기채 ETF ══
    ("UUP", "Invesco DB US Dollar Bullish", "AMEX"),
    ("FXY", "Invesco CurrencyShares Yen", "AMEX"),
    ("FXE", "Invesco CurrencyShares Euro", "AMEX"),
    ("UDN", "Invesco DB US Dollar Bearish", "AMEX"),
    ("SHV", "iShares Short Treasury Bond", "AMEX"),
    ("BIL", "SPDR 1-3 Month T-Bill", "AMEX"),
    ("VGSH", "Vanguard Short-Term Treasury", "NASD"),
    ("VTIP", "Vanguard Short-Term Inflation", "NASD"),
    ("MUB", "iShares National Muni Bond", "AMEX"),
    ("EMB", "iShares JPMorgan EM Bond", "NASD"),
    ("BND", "Vanguard Total Bond Market", "NASD"),

    # ══ 추가 글로벌/지역 ETF ══
    ("VGK", "Vanguard FTSE Europe", "AMEX"),
    ("EFA", "iShares MSCI EAFE", "AMEX"),
    ("EWZ", "iShares MSCI Brazil", "NYSE"),
    ("EWW", "iShares MSCI Mexico", "NYSE"),
    ("EWA", "iShares MSCI Australia", "NYSE"),
    ("EWC", "iShares MSCI Canada", "NYSE"),
    ("EWS", "iShares MSCI Singapore", "NYSE"),
    ("EWT", "iShares MSCI Taiwan", "NASD"),
    ("EWQ", "iShares MSCI France", "NYSE"),
    ("EWP", "iShares MSCI Spain", "NYSE"),
    ("EZA", "iShares MSCI South Africa", "NYSE"),
    ("ASHR", "Xtrackers China A-Shares", "AMEX"),
    ("ILF", "iShares Latin America 40", "NYSE"),
    ("DEM", "WisdomTree EM High Dividend", "AMEX"),
    ("DXJ", "WisdomTree Japan Hedged", "NASD"),

    # ══ 추가 저가 가치주 ($50 이하) ══
    ("AAL", "American Airlines", "NASD"),
    ("UAL", "United Airlines", "NASD"),
    ("CCL", "Carnival", "NYSE"),
    ("RCL", "Royal Caribbean", "NYSE"),
    ("NCLH", "Norwegian Cruise", "NYSE"),
    ("MRO", "Marathon Oil", "NYSE"),
    ("DVN", "Devon Energy", "NYSE"),
    ("APA", "APA Corp", "NASD"),
    ("MOS", "Mosaic", "NYSE"),
    ("CF", "CF Industries", "NYSE"),
    ("DD", "DuPont", "NYSE"),
    ("OXY", "Occidental", "NYSE"),
    ("SU", "Suncor Energy", "NYSE"),
    ("CNQ", "Canadian Natural Resources", "NYSE"),
    ("SHEL", "Shell", "NYSE"),
    ("BP", "BP", "NYSE"),
    ("E", "Eni SpA", "NYSE"),
    ("ABEV", "Ambev", "NYSE"),
    ("ITUB", "Itau Unibanco", "NYSE"),
    ("BBD", "Banco Bradesco", "NYSE"),
    ("VALE", "Vale", "NYSE"),
    ("RIG", "Transocean", "NYSE"),
    ("CHK", "Chesapeake Energy", "NASD"),
    ("EQT", "EQT Corp", "NYSE"),
    ("PARA", "Paramount Global", "NASD"),
    ("WBD", "Warner Bros Discovery", "NASD"),
    ("DIS", "Disney", "NYSE"),  # ~$100 borderline
    ("GE", "General Electric", "NYSE"),
    ("BA", "Boeing", "NYSE"),  # ~$170 borderline
    ("KSS", "Kohl's", "NYSE"),
    ("M", "Macy's", "NYSE"),
    ("GPS", "Gap", "NYSE"),
    ("URBN", "Urban Outfitters", "NASD"),
    ("BBY", "Best Buy", "NYSE"),
    ("ROST", "Ross Stores", "NASD"),
    ("EBAY", "eBay", "NASD"),
    ("YELP", "Yelp", "NYSE"),
    ("RKT", "Rocket Companies", "NYSE"),
    ("SOFI", "SoFi", "NASD"),
    ("UPST", "Upstart", "NASD"),
    ("AFRM", "Affirm", "NASD"),
    ("WISH", "ContextLogic", "NASD"),
    ("BBBY", "Bed Bath & Beyond", "NASD"),
    ("BB", "BlackBerry", "NYSE"),
    ("FUBO", "fuboTV", "NYSE"),
    ("DKNG", "DraftKings", "NASD"),
    ("PENN", "PENN Entertainment", "NASD"),
    ("MGM", "MGM Resorts", "NYSE"),
    ("WYNN", "Wynn Resorts", "NASD"),
    ("LVS", "Las Vegas Sands", "NYSE"),

    # ══ S&P 500 추가 (저가 우량주) ══
    ("AES", "AES Corp", "NYSE"),
    ("AFL", "Aflac", "NYSE"),
    ("AKAM", "Akamai", "NASD"),
    ("ALB", "Albemarle", "NYSE"),
    ("ALGN", "Align Technology", "NASD"),
    ("ALL", "Allstate", "NYSE"),
    ("AMAT", "Applied Materials", "NASD"),
    ("AMCR", "Amcor", "NYSE"),
    ("AMGN", "Amgen", "NASD"),
    ("ANET", "Arista Networks", "NYSE"),
    ("ANSS", "ANSYS", "NASD"),
    ("AON", "Aon", "NYSE"),
    ("AOS", "A.O. Smith", "NYSE"),
    ("APD", "Air Products", "NYSE"),
    ("APH", "Amphenol", "NYSE"),
    ("APTV", "Aptiv", "NYSE"),
    ("ARE", "Alexandria Real Estate", "NYSE"),
    ("ATO", "Atmos Energy", "NYSE"),
    ("AVB", "AvalonBay", "NYSE"),
    ("AVY", "Avery Dennison", "NYSE"),
    ("AWK", "American Water Works", "NYSE"),
    ("AXP", "American Express", "NYSE"),
    ("AZO", "AutoZone", "NYSE"),
    ("BALL", "Ball Corp", "NYSE"),
    ("BAX", "Baxter International", "NYSE"),
    ("BDX", "Becton Dickinson", "NYSE"),
    ("BEN", "Franklin Resources", "NYSE"),
    ("BIO", "Bio-Rad Labs", "NYSE"),
    ("BKR", "Baker Hughes", "NASD"),
    ("BLK", "BlackRock", "NYSE"),
    ("BR", "Broadridge Financial", "NYSE"),
    ("BRO", "Brown & Brown", "NYSE"),
    ("BSX", "Boston Scientific", "NYSE"),
    ("BWA", "BorgWarner", "NYSE"),
    ("BXP", "Boston Properties", "NYSE"),
    ("CAG", "Conagra", "NYSE"),
    ("CAH", "Cardinal Health", "NYSE"),
    ("CARR", "Carrier Global", "NYSE"),
    ("CB", "Chubb", "NYSE"),
    ("CBOE", "Cboe Global", "AMEX"),
    ("CBRE", "CBRE Group", "NYSE"),
    ("CDNS", "Cadence Design", "NASD"),
    ("CDW", "CDW Corp", "NASD"),
    ("CE", "Celanese", "NYSE"),
    ("CHRW", "C.H. Robinson", "NASD"),
    ("CHTR", "Charter Communications", "NASD"),
    ("CINF", "Cincinnati Financial", "NASD"),
    ("CL", "Colgate-Palmolive", "NYSE"),
    ("CMG", "Chipotle", "NYSE"),
    ("CMI", "Cummins", "NYSE"),
    ("CMS", "CMS Energy", "NYSE"),
    ("CNC", "Centene", "NYSE"),
    ("COG", "Cabot Oil & Gas", "NYSE"),
    ("COO", "Cooper Companies", "NYSE"),
    ("COP", "ConocoPhillips", "NYSE"),
    ("COST", "Costco", "NASD"),
    ("CPB", "Campbell Soup", "NYSE"),
    ("CPRT", "Copart", "NASD"),
    ("CRM", "Salesforce", "NYSE"),
    ("CSX", "CSX Corp", "NASD"),
    ("CTAS", "Cintas", "NASD"),
    ("CTLT", "Catalent", "NYSE"),
    ("CTRA", "Coterra Energy", "NYSE"),
    ("CTSH", "Cognizant", "NASD"),
    ("CTVA", "Corteva", "NYSE"),
    ("DAL", "Delta", "NYSE"),
    ("DD", "DuPont", "NYSE"),
    ("DE", "Deere", "NYSE"),
    ("DG", "Dollar General", "NYSE"),
    ("DGX", "Quest Diagnostics", "NYSE"),
    ("DHI", "D.R. Horton", "NYSE"),
    ("DHR", "Danaher", "NYSE"),
    ("DLR", "Digital Realty", "NYSE"),
    ("DLTR", "Dollar Tree", "NASD"),
    ("DOV", "Dover", "NYSE"),
    ("DPZ", "Domino's Pizza", "NYSE"),
    ("DRE", "Duke Realty", "NYSE"),
    ("DRI", "Darden Restaurants", "NYSE"),
    ("DTE", "DTE Energy", "NYSE"),
    ("DUK", "Duke Energy", "NYSE"),
    ("ECL", "Ecolab", "NYSE"),
    ("EFX", "Equifax", "NYSE"),
    ("EIX", "Edison International", "NYSE"),
    ("EL", "Estée Lauder", "NYSE"),
    ("EMN", "Eastman Chemical", "NYSE"),
    ("EMR", "Emerson Electric", "NYSE"),
    ("ENPH", "Enphase Energy", "NASD"),
    ("EOG", "EOG Resources", "NYSE"),
    ("EPAM", "EPAM Systems", "NYSE"),
    ("EQR", "Equity Residential", "NYSE"),
    ("ESS", "Essex Property Trust", "NYSE"),
    ("ETN", "Eaton", "NYSE"),
    ("ETR", "Entergy", "NYSE"),
    ("EW", "Edwards Lifesciences", "NYSE"),
    ("EXC", "Exelon", "NASD"),
    ("EXPD", "Expeditors", "NASD"),
    ("EXPE", "Expedia", "NASD"),
    ("EXR", "Extra Space Storage", "NYSE"),
    ("FAST", "Fastenal", "NASD"),
    ("FDS", "FactSet", "NYSE"),
    ("FE", "FirstEnergy", "NYSE"),
    ("FFIV", "F5 Networks", "NASD"),
    ("FIS", "FIS", "NYSE"),
    ("FISV", "Fiserv", "NASD"),
    ("FITB", "Fifth Third", "NASD"),
    ("FLT", "FleetCor", "NYSE"),
    ("FMC", "FMC Corp", "NYSE"),
    ("FOX", "Fox Corp", "NASD"),
    ("FOXA", "Fox Corp Class A", "NASD"),
    ("FRT", "Federal Realty", "NYSE"),
    ("FTNT", "Fortinet", "NASD"),
    ("FTV", "Fortive", "NYSE"),
    ("GD", "General Dynamics", "NYSE"),
    ("GIS", "General Mills", "NYSE"),
    ("GLW", "Corning", "NYSE"),
    ("GM", "General Motors", "NYSE"),
    ("GNRC", "Generac", "NYSE"),
    ("GPN", "Global Payments", "NYSE"),
    ("GRMN", "Garmin", "NYSE"),
    ("GS", "Goldman Sachs", "NYSE"),
    ("HAS", "Hasbro", "NASD"),
    ("HBI", "Hanesbrands", "NYSE"),
    ("HCA", "HCA Healthcare", "NYSE"),
    ("HD", "Home Depot", "NYSE"),
    ("HES", "Hess Corp", "NYSE"),
    ("HIG", "Hartford Financial", "NYSE"),
    ("HII", "Huntington Ingalls", "NYSE"),
    ("HLT", "Hilton Worldwide", "NYSE"),
    ("HOLX", "Hologic", "NASD"),
    ("HON", "Honeywell", "NASD"),
    ("HPE", "HP Enterprise", "NYSE"),
    ("HRL", "Hormel Foods", "NYSE"),
    ("HST", "Host Hotels", "NASD"),
    ("HSY", "Hershey", "NYSE"),
    ("HUBB", "Hubbell", "NYSE"),
    ("ICE", "Intercontinental Exchange", "NYSE"),
    ("IDXX", "IDEXX Labs", "NASD"),
    ("IEX", "IDEX Corp", "NYSE"),
    ("IFF", "International Flavors", "NYSE"),
    ("IP", "International Paper", "NYSE"),
    ("IPG", "Interpublic Group", "NYSE"),
    ("IPGP", "IPG Photonics", "NASD"),
    ("IQV", "IQVIA", "NYSE"),
    ("IRM", "Iron Mountain", "NYSE"),
    ("ISRG", "Intuitive Surgical", "NASD"),
    ("IT", "Gartner", "NYSE"),
    ("IVZ", "Invesco", "NYSE"),
    ("J", "Jacobs Engineering", "NYSE"),
    ("JBHT", "J.B. Hunt", "NASD"),
    ("JBL", "Jabil", "NYSE"),
    ("JCI", "Johnson Controls", "NYSE"),
    ("JKHY", "Jack Henry", "NASD"),
    ("JNPR", "Juniper Networks", "NYSE"),
    ("K", "Kellogg", "NYSE"),
    ("KEYS", "Keysight Technologies", "NYSE"),
    ("KIM", "Kimco Realty", "NYSE"),
    ("KLAC", "KLA Corp", "NASD"),
    ("KMB", "Kimberly-Clark", "NYSE"),
    ("KMX", "CarMax", "NYSE"),
    ("KO", "Coca-Cola", "NYSE"),
    ("L", "Loews Corp", "NYSE"),
    ("LDOS", "Leidos", "NYSE"),
    ("LEN", "Lennar", "NYSE"),
    ("LH", "LabCorp", "NYSE"),
    ("LHX", "L3Harris", "NYSE"),
    ("LIN", "Linde", "NYSE"),
    ("LKQ", "LKQ Corp", "NASD"),
    ("LLY", "Eli Lilly", "NYSE"),  # 비싼 종목
    ("LMT", "Lockheed Martin", "NYSE"),  # 비싼 종목
    ("LNC", "Lincoln National", "NYSE"),
    ("LNT", "Alliant Energy", "NASD"),
    ("LOW", "Lowe's", "NYSE"),
    ("LRCX", "Lam Research", "NASD"),
    ("LUV", "Southwest", "NYSE"),
    ("LW", "Lamb Weston", "NYSE"),
    ("LYB", "LyondellBasell", "NYSE"),
    ("MAA", "Mid-America Apartment", "NYSE"),
    ("MAR", "Marriott", "NASD"),
    ("MAS", "Masco", "NYSE"),
    ("MCD", "McDonald's", "NYSE"),
    ("MCHP", "Microchip", "NASD"),
    ("MCK", "McKesson", "NYSE"),
    ("MDLZ", "Mondelez", "NASD"),
    ("MDT", "Medtronic", "NYSE"),
    ("MET", "MetLife", "NYSE"),
    ("MGM", "MGM Resorts", "NYSE"),
    ("MKC", "McCormick", "NYSE"),
    ("MLM", "Martin Marietta", "NYSE"),
    ("MMC", "Marsh McLennan", "NYSE"),
    ("MMM", "3M", "NYSE"),
    ("MNST", "Monster Beverage", "NASD"),
    ("MOH", "Molina Healthcare", "NYSE"),
    ("MPC", "Marathon Petroleum", "NYSE"),
    ("MPWR", "Monolithic Power", "NASD"),
    ("MRO", "Marathon Oil", "NYSE"),
    ("MS", "Morgan Stanley", "NYSE"),
    ("MSCI", "MSCI", "NYSE"),
    ("MSI", "Motorola Solutions", "NYSE"),
    ("MTB", "M&T Bank", "NYSE"),
    ("MTCH", "Match Group", "NASD"),
    ("MTD", "Mettler-Toledo", "NYSE"),
    ("NDAQ", "Nasdaq", "NASD"),
    ("NDSN", "Nordson", "NASD"),
    ("NEE", "NextEra Energy", "NYSE"),
    ("NEM", "Newmont", "NYSE"),
    ("NI", "NiSource", "NYSE"),
    ("NKE", "Nike", "NYSE"),
    ("NLOK", "NortonLifeLock", "NASD"),
    ("NOC", "Northrop Grumman", "NYSE"),
    ("NOW", "ServiceNow", "NYSE"),  # 비싼 종목
    ("NRG", "NRG Energy", "NYSE"),
    ("NSC", "Norfolk Southern", "NYSE"),
    ("NTRS", "Northern Trust", "NASD"),
    ("NUE", "Nucor", "NYSE"),
    ("NVR", "NVR", "NYSE"),  # 비싼 종목
    ("NWL", "Newell Brands", "NASD"),
    ("NXPI", "NXP Semiconductors", "NASD"),
    ("O", "Realty Income", "NYSE"),
    ("ODFL", "Old Dominion Freight", "NASD"),
    ("OKE", "ONEOK", "NYSE"),
    ("OMC", "Omnicom Group", "NYSE"),
    ("ORCL", "Oracle", "NYSE"),
    ("ORLY", "O'Reilly", "NASD"),
    ("PAYC", "Paycom", "NYSE"),
    ("PAYX", "Paychex", "NASD"),
    ("PCAR", "PACCAR", "NASD"),
    ("PEG", "Public Service Enterprise", "NYSE"),
    ("PEP", "PepsiCo", "NASD"),
    ("PG", "Procter & Gamble", "NYSE"),
    ("PGR", "Progressive", "NYSE"),
    ("PH", "Parker Hannifin", "NYSE"),
    ("PHM", "PulteGroup", "NYSE"),
    ("PKI", "PerkinElmer", "NYSE"),
    ("PLD", "Prologis", "NYSE"),
    ("PNC", "PNC Financial", "NYSE"),
    ("PNR", "Pentair", "NYSE"),
    ("PNW", "Pinnacle West Capital", "NYSE"),
    ("POOL", "Pool Corp", "NASD"),
    ("PPG", "PPG Industries", "NYSE"),
    ("PPL", "PPL Corp", "NYSE"),
    ("PRU", "Prudential", "NYSE"),
    ("PSA", "Public Storage", "NYSE"),
    ("PSX", "Phillips 66", "NYSE"),
    ("PWR", "Quanta Services", "NYSE"),
    ("PXD", "Pioneer Natural Resources", "NYSE"),
    ("PYPL", "PayPal", "NASD"),
    ("QCOM", "Qualcomm", "NASD"),
    ("QRVO", "Qorvo", "NASD"),
    ("RCL", "Royal Caribbean", "NYSE"),
    ("REG", "Regency Centers", "NASD"),
    ("REGN", "Regeneron", "NASD"),
    ("RHI", "Robert Half", "NYSE"),
    ("RJF", "Raymond James", "NYSE"),
    ("RL", "Ralph Lauren", "NYSE"),
    ("RMD", "ResMed", "NYSE"),
    ("ROK", "Rockwell Automation", "NYSE"),
    ("ROL", "Rollins", "NYSE"),
    ("ROP", "Roper Technologies", "NYSE"),
    ("ROST", "Ross Stores", "NASD"),
    ("RSG", "Republic Services", "NYSE"),
    ("RTX", "Raytheon Technologies", "NYSE"),
    ("SBAC", "SBA Communications", "NASD"),
    ("SBUX", "Starbucks", "NASD"),
    ("SCHW", "Charles Schwab", "NYSE"),
    ("SEDG", "SolarEdge", "NASD"),
    ("SEE", "Sealed Air", "NYSE"),
    ("SHW", "Sherwin-Williams", "NYSE"),
    ("SIVB", "SVB Financial", "NASD"),
    ("SJM", "J.M. Smucker", "NYSE"),
    ("SNA", "Snap-on", "NYSE"),
    ("SNPS", "Synopsys", "NASD"),
    ("SO", "Southern Company", "NYSE"),
    ("SPG", "Simon Property Group", "NYSE"),
    ("SPGI", "S&P Global", "NYSE"),
    ("SRE", "Sempra Energy", "NYSE"),
    ("STE", "STERIS", "NYSE"),
    ("STT", "State Street", "NYSE"),
    ("STX", "Seagate", "NASD"),
    ("STZ", "Constellation Brands", "NYSE"),
    ("SWK", "Stanley Black & Decker", "NYSE"),
    ("SWKS", "Skyworks Solutions", "NASD"),
    ("SYF", "Synchrony Financial", "NYSE"),
    ("SYK", "Stryker", "NYSE"),
    ("SYY", "Sysco", "NYSE"),
    ("TAP", "Molson Coors", "NYSE"),
    ("TDG", "TransDigm", "NYSE"),
    ("TDY", "Teledyne Technologies", "NYSE"),
    ("TEL", "TE Connectivity", "NYSE"),
    ("TER", "Teradyne", "NASD"),
    ("TFX", "Teleflex", "NYSE"),
    ("TGT", "Target", "NYSE"),
    ("TMO", "Thermo Fisher", "NYSE"),
    ("TMUS", "T-Mobile", "NASD"),
    ("TROW", "T. Rowe Price", "NASD"),
    ("TRV", "Travelers", "NYSE"),
    ("TSCO", "Tractor Supply", "NASD"),
    ("TSN", "Tyson Foods", "NYSE"),
    ("TT", "Trane Technologies", "NYSE"),
    ("TTWO", "Take-Two Interactive", "NASD"),
    ("TXN", "Texas Instruments", "NASD"),
    ("TXT", "Textron", "NYSE"),
    ("TYL", "Tyler Technologies", "NYSE"),
    ("UDR", "UDR", "NYSE"),
    ("UHS", "Universal Health", "NYSE"),
    ("UNH", "UnitedHealth", "NYSE"),  # 비싼 종목
    ("UNP", "Union Pacific", "NYSE"),
    ("UPS", "UPS", "NYSE"),
    ("URI", "United Rentals", "NYSE"),
    ("V", "Visa", "NYSE"),
    ("VFC", "VF Corp", "NYSE"),
    ("VLO", "Valero Energy", "NYSE"),
    ("VMC", "Vulcan Materials", "NYSE"),
    ("VRSK", "Verisk", "NASD"),
    ("VRTX", "Vertex Pharma", "NASD"),
    ("VTR", "Ventas", "NYSE"),
    ("WAB", "Westinghouse Air Brake", "NYSE"),
    ("WAT", "Waters Corp", "NYSE"),
    ("WEC", "WEC Energy", "NYSE"),
    ("WELL", "Welltower", "NYSE"),
    ("WHR", "Whirlpool", "NYSE"),
    ("WLTW", "Willis Towers Watson", "NASD"),
    ("WM", "Waste Management", "NYSE"),
    ("WMT", "Walmart", "NYSE"),
    ("WRB", "W.R. Berkley", "NYSE"),
    ("WRK", "WestRock", "NYSE"),
    ("WST", "West Pharmaceutical", "NYSE"),
    ("WY", "Weyerhaeuser", "NYSE"),
    ("WYNN", "Wynn Resorts", "NASD"),
    ("XEL", "Xcel Energy", "NASD"),
    ("XYL", "Xylem", "NYSE"),
    ("YUM", "Yum! Brands", "NYSE"),
    ("ZBH", "Zimmer Biomet", "NYSE"),
    ("ZBRA", "Zebra Technologies", "NASD"),
    ("ZTS", "Zoetis", "NYSE"),
]
US_CANDIDATE_POOL = []
_seen_us = set()
for _t, _n, _e in _US_RAW:
    if _t not in _seen_us:
        _seen_us.add(_t)
        US_CANDIDATE_POOL.append((_t, _n, _e))
del _US_RAW, _seen_us, _KR_RAW, _seen_kr


class AutoScreener:
    """후보 풀 자동 스크리닝."""

    def __init__(self, kr_pick: int = 5, us_pick: int = 5,
                 min_p_profit: float = 0.55,
                 min_var95: float = -0.15):
        self.kr_pick = kr_pick
        self.us_pick = us_pick
        self.min_p_profit = min_p_profit
        self.min_var95 = min_var95

    @staticmethod
    def _score(mc: dict) -> float:
        """MC 결과 → 종목 점수.

        가중치: P(profit>0) 50% + 평균수익 30% + VaR(95%) 20% (위험은 -)
        """
        if not mc:
            return -999.0
        p = mc.get("p_profit", 0)
        m = mc.get("mean_profit", 0)
        v = mc.get("var95", 0)  # 음수
        return p * 0.5 + m * 5 * 0.3 + v * 0.2  # var95 가중

    @staticmethod
    def _score_alt(method: str, close, returns) -> float:
        """MC 외 종목 점수 — close(가격)/returns(일수익률) 기반. 시뮬 없이 빠르게.

        momentum : 20일·60일 모멘텀 가중 (강세 추격)
        trend    : 정배열(ma5>ma20>ma60) + 60일선 위 거리 (추세 추종)
        low_vol  : 저변동(방어) 우선 + 약한 양의 추세 (하락장 적합)
        """
        try:
            cur = float(close[-1])
            m20 = cur / float(close[-21]) - 1.0 if len(close) > 21 else 0.0
            if method == "momentum":
                m60 = cur / float(close[-61]) - 1.0 if len(close) > 61 else 0.0
                return 0.7 * m20 + 0.3 * m60
            if method == "trend":
                if len(close) < 60:
                    return 0.0
                ma5 = float(np.mean(close[-5:]))
                ma20 = float(np.mean(close[-20:]))
                ma60 = float(np.mean(close[-60:]))
                align = (1.0 if ma5 > ma20 else 0.0) + (1.0 if ma20 > ma60 else 0.0)
                above = (cur - ma60) / ma60 if ma60 else 0.0
                return align + max(-0.2, min(0.5, above))
            if method == "low_vol":
                vol = float(np.std(returns[-20:])) if len(returns) >= 20 else float(np.std(returns) or 0.01)
                return (-vol) + 0.3 * m20
            return m20  # 알 수 없는 method → 모멘텀 폴백
        except Exception:
            return -999.0

    def screen_market(self, candidates: list, ohlcv_fetcher,
                      mc_runner=None, n_paths: int = 10000,
                      max_workers: int = 8, method: str = "monte_carlo") -> list[dict]:
        """단일 시장(KR/US)의 모든 후보 평가 → 상위 N개 정렬 결과.

        method: 종목 점수 방식 — "monte_carlo"(기본, MC 부트스트랩) | "momentum" | "trend" | "low_vol".
                MC 외 방식은 시뮬 없이 가격/수익률로 점수 → 더 빠르고 상황별로 골라 쓸 수 있다.
        mc_runner: 하위호환용(무시, numpy MC 사용).
        max_workers: OHLCV 병렬 fetch 워커 수 (KIS rate limit 18/sec 호환).
        """
        from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                         TimeoutError as FuturesTimeout)
        # Python 3.8: concurrent.futures.TimeoutError ≠ 빌트인 TimeoutError (3.11에서 통합).
        # as_completed(timeout=)은 전자를 던지므로 반드시 FuturesTimeout으로 잡아야 한다.
        # Vortex 제거 — 종목 평가 MC는 numpy. mc_runner 인자는 하위호환용(무시).
        from zusik.analysis.bot_money_helpers import monte_carlo_bootstrap_numpy as _mc_numpy

        # ── 1단계: OHLCV 병렬 fetch (대규모 풀에서 큰 시간 절약) ──
        ohlcv_cache: dict = {}

        def _fetch_one(cand):
            code = cand[0]
            try:
                df = ohlcv_fetcher(*cand)
                return code, df
            except Exception:
                return code, None

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_fetch_one, c): c for c in candidates}
            #: KR 250봉 + rate-limit 경합 여유로 240s.: 타임아웃 시
            # 전체를 버리지 않고(이전엔 '자동 스크리닝 오류'로 전파) 그때까지 받은 부분 결과로
            # 진행 — 대형 풀(585종목)이 마감 못 해도 완료분만 평가한다.
            try:
                for fut in as_completed(futures, timeout=240):
                    try:
                        code, df = fut.result()
                        if df is not None:
                            ohlcv_cache[code] = df
                    except Exception:
                        pass
            except FuturesTimeout:
                logger.warning("선별 fetch 부분 완료: %d/%d종목 (타임아웃 240s) — 완료분으로 진행",
                               len(ohlcv_cache), len(candidates))

        results = []
        for cand in candidates:
            code = cand[0]
            try:
                df = ohlcv_cache.get(code)
                if df is None or len(df) < 30:
                    continue
                close = df["close"].astype(float).values
                returns = (close[1:] / close[:-1] - 1.0).astype(np.float32)
                if len(returns) < 20:
                    continue
                hist = returns[-60:] if len(returns) >= 60 else returns

                # 종목 점수: 방식 선택 (MC | momentum | trend | low_vol)
                if method == "monte_carlo":
                    mc = _mc_numpy(
                        hist, n_paths=min(n_paths, 2000), t_forward=30,
                        stop_loss=-0.10, trailing_stop=-0.05, target_profit=0.10,
                    )
                    if mc is None:
                        continue
                    score = self._score(mc)
                else:
                    mc = None
                    score = self._score_alt(method, close, returns)
                # 추세 점수 (5/20/60일선)
                trend_ok = True
                if len(close) >= 60:
                    ma5 = float(np.mean(close[-5:]))
                    ma20 = float(np.mean(close[-20:]))
                    ma60 = float(np.mean(close[-60:]))
                    cur = float(close[-1])
                    # 데드크로스 + 60일선 아래 = 약세 → 후보 제외
                    if ma5 < ma20 and cur < ma60:
                        trend_ok = False
                results.append({
                    "code": code,
                    "info": cand,
                    "mc": mc,
                    "method": method,
                    "score": score,
                    "trend_ok": trend_ok,
                    "last_price": float(close[-1]),
                })
            except Exception as e:
                logger.debug("스크리닝 실패 %s: %s", code, e)
        # 점수 내림차순 정렬
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    # 한국 ETF 운용사 접두어. 이 prefix로 시작하면 ETF로 판정 (단일주가 아님).
    _ETF_NAME_PREFIXES_KR = (
        "TIGER", "KODEX", "ACE", "SOL", "KBSTAR", "KOSEF",
        "RISE", "HANARO", "ARIRANG", "SMART", "KIWOOM", "HK ",
    )
    # 미국 ETF 키워드 (소문자 비교).
    _ETF_NAME_KEYWORDS_US = (
        " etf", " trust", " fund", "ishares", "vanguard ",
        "spdr ", "proshares", "invesco ", "direxion",
    )

    @classmethod
    def _is_single_stock(cls, info: tuple) -> bool:
        """info=(code/ticker, name, ...) → ETF/펀드/트러스트면 False, 단일주면 True."""
        name = (info[1] if len(info) > 1 else "") or ""
        name_upper = name.upper()
        if any(name_upper.startswith(p) for p in cls._ETF_NAME_PREFIXES_KR):
            return False
        name_lower = name.lower()
        if any(kw in name_lower for kw in cls._ETF_NAME_KEYWORDS_US):
            return False
        return True

    def filter_top(self, scored: list[dict], pick: int,
                   include_inverse: bool = True,
                   max_price: float = 0,
                   min_single_stocks: int = 0) -> list[dict]:
        """상위 N 선정 — 추세 + 임계 통과만.

        max_price > 0이면 last_price <= max_price 종목만 (소액 계좌 가격 캡).
        min_single_stocks > 0이면 ETF만 채워지지 않도록 단일주 슬롯 강제 보장.
        """
        # 통과 기준 평가
        def _passes(r):
            if max_price > 0:
                lp = r.get("last_price", 0)
                if lp > 0 and lp > max_price:
                    return False
            if not r["trend_ok"]:
                name = (r["info"][1] if len(r["info"]) > 1 else "").lower()
                if "인버스" not in name and "short" not in name and "inverse" not in name:
                    return False
                if not include_inverse:
                    return False
            mc = r.get("mc")
            if mc is not None:
                if mc["p_profit"] < self.min_p_profit and "인버스" not in (r["info"][1] if len(r["info"]) > 1 else ""):
                    return False
                if mc["var95"] < self.min_var95:
                    return False
            elif r.get("method") == "momentum" and float(r.get("score", 0)) <= 0:
                # 모멘텀 모드에서 횡보/하락 후보로 현금을 소진하지 않는다.
                return False
            return True

        valid = [r for r in scored if _passes(r)]

        if min_single_stocks <= 0:
            return valid[:pick]

        # 단일주 슬롯 강제 — 단일주를 먼저 min_single_stocks개 채우고 나머지는 점수순.
        singles = [r for r in valid if self._is_single_stock(r["info"])]
        [r for r in valid if not self._is_single_stock(r["info"])]
        n_single = min(min_single_stocks, len(singles), pick)
        out = singles[:n_single]
        remaining_slots = pick - n_single
        # 남은 슬롯은 단일주+ETF 점수순으로 채움 (이미 뽑힌 단일주 제외)
        rest_pool = [r for r in valid if r not in out]
        out.extend(rest_pool[:remaining_slots])
        return out
