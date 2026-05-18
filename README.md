# Market Pulse — GitHub Pages 무료 자동 업데이트 버전

Flask/SQLite/DART/린치·버핏 탭을 제거하고, GitHub Pages + GitHub Actions로 장마감 후 `data/latest.json`을 갱신하는 구조입니다.

## 구조

```txt
index.html                    # 정적 대시보드
scripts/collect.py            # pykrx 기반 데이터 수집 → data/latest.json 생성
data/latest.json              # 화면이 읽는 최신 데이터
.github/workflows/update-data.yml
requirements.txt
.nojekyll
```

## 자동 업데이트 시간

평일 한국시간 기준:

```txt
18:00 1차 시도
18:10 2차 시도
18:20 3차 시도
18:30 4차 시도
18:40 5차 시도
18:50 6차 시도
19:00 최종 확인
```

이미 같은 거래일의 핵심 데이터가 정상 저장되어 있으면 이후 재시도 실행은 `Already collected fresh data...` 메시지를 남기고 바로 종료됩니다.

## 사용 방법

1. GitHub에 새 public repository를 만듭니다.
2. 이 폴더 안 파일을 그대로 업로드합니다.
3. Repository Settings → Pages → Deploy from branch → `main` / `/root` 선택.
4. Actions 탭에서 `Update market data`를 수동 실행해 첫 데이터를 만듭니다.
5. 이후 평일 한국시간 18:00~19:00 사이 10분 간격으로 자동 업데이트를 시도합니다.

## 중요한 점

- `index.html`은 `data/latest.json`만 읽습니다. 서버, DB, Flask가 필요 없습니다.
- 수급/가격/외인지분율은 `pykrx`를 통해 KRX 데이터를 가져옵니다.
- 신용잔고는 pykrx/공개 소스에서 지원되는 함수가 잡히면 표시하고, 실패하면 가짜값을 넣지 않고 `수집 불가`로 표시합니다.
- 투자 권유가 아니라 참고용 대시보드입니다.

## 로컬 테스트

```bash
pip install -r requirements.txt
python scripts/collect.py
python -m http.server 8000
```

브라우저에서 `http://localhost:8000` 접속.
