.PHONY: doctor plan test

doctor:
	./bin/axis-release doctor

plan:
	./bin/axis-release plan --app example --launch example

test:
	python3 -m compileall -q axis stages
	python3 -m unittest discover -s tests -p 'test_*.py' -v
