# stock-terminal

관심 종목(한국/미국)을 실시간으로 모니터링하는 터미널 프로그램. [토스증권 Open API](https://developers.tossinvest.com/docs)를 데이터 소스로 사용한다.

## 사전 준비: 토스증권 Open API 발급

1. 토스증권 계좌가 없다면 먼저 개설한다.
2. 토스증권 WTS에 로그인 → **설정 > Open API** 메뉴로 이동해 `client_id` / `client_secret`을 발급받는다.
3. 같은 화면 하단 **허용 IP 관리**에서 이 프로그램을 실행할 PC(또는 서버)의 공인 IP를 등록한다.
   - 등록하지 않으면 모든 API 호출이 `403 Forbidden`으로 거부된다.
4. 참고: 이 API는 아직 WebSocket을 지원하지 않는다 (문서상 "추후 지원 예정"). 이 앱은 REST 폴링 방식으로 실시간성을 구현한다.

## 설치

```bash
uv sync
```

## 설정

프로젝트 루트에 `.env` 파일을 만든다 (`.env.example` 참고):

```
TOSS_CLIENT_ID=발급받은_client_id
TOSS_CLIENT_SECRET=발급받은_client_secret

# 선택: 폴링 주기(초), 차트 갱신 주기(초)
POLL_INTERVAL_SECONDS=3
CHART_REFRESH_SECONDS=30
```

## 실행

```bash
uv run stock-terminal
# 또는
uv run python -m stock_terminal
```

`.env`가 없거나 값이 비어있으면 앱은 실행되지 않고 설정 방법을 안내하는 메시지를 출력한다.

## 키 조작

| 키 | 동작 |
|----|------|
| `a` | 종목 추가 (심볼 코드 입력 → 유효성 확인 후 관심목록에 추가) |
| `d` | 선택한 종목을 관심목록에서 삭제 |
| `t` | 선택한 종목의 알림 상한가/하한가 설정 |
| `m` | 차트 모드 전환 (캔들 ↔ 라인) |
| `i` | 차트 봉 주기 전환 (일봉 ↔ 1분봉) |
| `q` | 종료 |

종목 코드는 국내는 6자리 숫자(예: `005930`), 미국은 티커(예: `AAPL`)를 그대로 입력한다. 별도 검색 API가 없어 정확한 코드를 알아야 하며, `a`로 추가할 때 `/api/v1/stocks` 조회 결과(종목명/시장)를 보여줘 오탈자를 확인할 수 있다.

## 관심목록 저장 위치

`~/.stock_terminal/watchlist.json` 에 종목/알림 설정이 저장되어 재실행 시 그대로 불러온다.

## 테스트

실제 API 자격증명 없이도 `respx`로 HTTP를 모킹해 인증/재시도/레이트리밋 로직을 검증할 수 있다:

```bash
uv run pytest
```
