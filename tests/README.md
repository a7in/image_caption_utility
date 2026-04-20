# Tests for Image Caption Utility

This directory contains automated tests for the project. 
The tests use the `pytest` framework and `pytest-cov` for generating test coverage reports.

## Setup

If you haven't already, install the required testing dependencies:

```bash
pip install pytest pytest-cov
```

## Running Tests

To run all test files (`test_db.py` and `test_main.py`), open your terminal in the root project directory and run:

```bash
pytest tests/
```

This will automatically discover and execute all functions starting with `test_` inside the `tests/` directory.

## Viewing Test Coverage

To check how much of the application's code is covered by these tests, use `--cov` and manually specify the project files (`main` and `db`) so the coverage report doesn't include the test files themselves:

```bash
pytest --cov=main --cov=db tests/

#to view in IDE with "Coverage Gutters" extension
pytest --cov=main --cov=db --cov-report=xml tests/
```

### Detailed HTML Report

If you want to see exactly which lines of code are tested and which are not, you can generate an HTML report:

```bash
pytest --cov=main --cov=db --cov-report=html tests/
```

After running this command, a folder named `htmlcov/` will be created in your project's root. Open `htmlcov/index.html` in your web browser to visually inspect your code coverage block-by-block.
