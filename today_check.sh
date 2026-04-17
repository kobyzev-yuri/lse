curl -sS "http://127.0.0.1:8080/api/analyzer?days=5&strategy=GAME_5M" > /tmp/analyzer_5d.json
jq '.summary, .meta.config_delta_from_previous' /tmp/analyzer_5d.json
cd /home/ai8049520/lse
python3 scripts/analyzer_autotune.py --days 5 --url http://127.0.0.1:8080/api/analyzer
# for cursor notice
