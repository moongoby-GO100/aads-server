# AADS 배포 명령 — docker compose 직접 사용 금지
.PHONY: deploy rebuild restart status

deploy:        ## 코드 반영 (기본)
	./deploy.sh code

rebuild:       ## 이미지 리빌드
	./deploy.sh build

restart:       ## 서비스 재시작
	./deploy.sh code

status:        ## 전체 상태 확인
	@docker ps --filter "name=aads" --format "table {{.Names}}\t{{.Status}}"

down:          ## ⚠️ 사용 금지
	@echo "⚠️  docker compose down은 사용 금지입니다."
	@echo "    코드 반영: make deploy"
	@echo "    리빌드:    make rebuild"

help:          ## 도움말
	@grep -E '^[a-z]+:.*##' Makefile | sed 's/:.*## /\t/'
