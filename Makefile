PYTHON ?= python3
PYTHONPATH := src

.PHONY: test compile shell-check check

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -v

compile:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m compileall -q src tests tools

shell-check:
	bash -n deploy/install.sh deploy/security-check.sh

check: compile test shell-check
