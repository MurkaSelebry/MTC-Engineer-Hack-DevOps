SHELL := /bin/bash
export PATH := $(CURDIR)/.venv/bin:$(PATH)
.PHONY: bootstrap preflight deploy verify test lint status package
bootstrap:
	./scripts/bootstrap.sh
preflight:
	./scripts/preflight.sh
deploy:
	./scripts/deploy.sh
verify:
	./scripts/verify.sh $(if $(VM_IP),--host $(VM_IP),)
test:
	python3 -m unittest discover -s tests -p 'test_*.py'
lint:
	./scripts/lint.sh
status:
	kubectl get nodes -o wide
	kubectl get pods -A
	kubectl -n demo get gateway,httproute
package:
	python3 scripts/package.py --repo-url "$(REPO_URL)" --name Резван --passport docs/passport/Паспорт.pdf --output dist
