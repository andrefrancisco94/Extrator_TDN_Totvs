.PHONY: help test test-cov lint clean install install-dev

help:
	@echo "Targets disponiveis:"
	@echo "  test         - Roda suite de testes (pytest)"
	@echo "  test-cov     - Roda testes com cobertura"
	@echo "  install      - Instala dependencias de producao"
	@echo "  install-dev  - Instala dependencias de dev (pytest, etc)"
	@echo "  clean        - Remove caches e arquivos temporarios"

test:
	.venv/Scripts/python.exe -m pytest tests/

test-cov:
	.venv/Scripts/python.exe -m pytest tests/ --cov=src --cov-report=term-missing --cov-report=html

install:
	.venv/Scripts/python.exe -m pip install -r requirements.txt

install-dev:
	.venv/Scripts/python.exe -m pip install -r requirements.txt
	.venv/Scripts/python.exe -m pip install pytest pytest-cov

clean:
	-find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
	-find . -type f -name "*.pyc" -delete
	-rm -rf .pytest_cache .mypy_cache htmlcov .coverage dist build *.egg-info
