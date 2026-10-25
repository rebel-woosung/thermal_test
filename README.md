# thermal_test

## 실행

```bash
unzip thermal_test.zip
cd thermal_test/
uv run python main.py   # sudo 비번 1회 입력
```

## 출력 (`log/<timestamp>/`):
- `server.log`
- `fan_control.csv`
- `server_monitoring.csv`
- `npu_monitoring_<bus>.csv` (Device별, 예: `npu_monitoring_0000-03-00.0.csv`)
