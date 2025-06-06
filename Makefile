.PHONY: test test-verbose test-file clean venv install coverage help activate deactivate

# Python configuration
PYTHON := python3
VENV_NAME := splitwise_venv
VENV_BIN := $(VENV_NAME)/bin
VENV_PYTHON := $(VENV_BIN)/python
PIP := $(VENV_BIN)/pip
COVERAGE := $(VENV_BIN)/coverage
REQUIREMENTS := requirements.txt

# Define a function to run commands in venv context
define run_in_venv
	@if [ -z "$$VIRTUAL_ENV" ]; then \
		. $(VENV_BIN)/activate >/dev/null 2>&1; \
		__VENV_ACTIVATED=1; \
	else \
		__VENV_ACTIVATED=0; \
	fi; \
	$(1); \
	status=$$?; \
	if [ $$__VENV_ACTIVATED -eq 1 ]; then \
		deactivate >/dev/null 2>&1; \
	fi; \
	exit $$status
endef

# Virtual environment target with dependency tracking
$(VENV_NAME)/pyvenv.cfg: $(REQUIREMENTS)
	$(PYTHON) -m venv $(VENV_NAME)
	$(call run_in_venv,$(PIP) install -r $(REQUIREMENTS))
	$(call run_in_venv,$(PIP) install coverage)
	@touch $(VENV_NAME)/pyvenv.cfg

venv: $(VENV_NAME)/pyvenv.cfg

install: venv

test: venv
	$(call run_in_venv,$(PYTHON) -m unittest discover -s . -p "test*.py")

test-verbose: venv
	$(call run_in_venv,$(PYTHON) -m unittest discover -s . -p "test*.py" -v)

test-file: venv
	$(call run_in_venv,$(PYTHON) -m unittest $(TEST_FILE))

coverage: venv
	-$(call run_in_venv,$(COVERAGE) run -m unittest discover -s . -p "test*.py")
	-$(call run_in_venv,$(COVERAGE) report)
	-$(call run_in_venv,$(COVERAGE) html)
	@echo "Coverage report generated in htmlcov/index.html"

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.pyd" -delete
	find . -type f -name ".coverage" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type d -name "*.egg" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name "htmlcov" -exec rm -rf {} +

clean-venv: clean
	rm -rf $(VENV_NAME)

deactivate:
	if [ ! -z "$$VIRTUAL_ENV" ]; then \
		deactivate; \
	else \
		echo "No virtual environment is currently active."; \
	fi

activate:
	@if [ -z "$$VIRTUAL_ENV" ]; then \
		echo "Activating virtual environment..."; \
		source $(VENV_BIN)/activate; \
	else \
		echo "Virtual environment is already active."; \
	fi

help:
	@echo "Available targets:"
	@echo "  test          - Run tests"
	@echo "  test-verbose  - Run tests with verbose output"
	@echo "  test-file     - Run a specific test file. Usage: make test-file TEST_FILE=tests/test_file.py"
	@echo "  coverage      - Run tests with coverage report"
	@echo "  clean         - Remove Python cache files and build artifacts"
	@echo "  clean-venv    - Remove virtual environment and clean all artifacts"
	@echo "  venv          - Create/update virtual environment and install dependencies"
	@echo "  install       - Alias for venv target"
	@echo "  activate      - Activate the virtual environment"
	@echo "  deactivate    - Deactivate the virtual environment"
	@echo "  help          - Show this help message"
