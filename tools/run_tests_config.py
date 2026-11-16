"""What tools/run_tests.py must know of PDF Cropper (the tool itself is a
copy of the model's, never edited here).

    python tools/run_tests.py tests                 the whole suite in one process, with the limit and the times
    python tools/run_tests.py --all --parallel 4    every test module as a process of its own, four at a time
    python tools/run_tests.py --changed             only what the files changed from main can touch
"""

# a test that has not ended after this many seconds stops the run with the stacks (600 by default, 0: never)
LIMIT_VARIABLE = "PDF_CROPPER_TEST_LIMIT"

# The choice of --changed: the first row whose prefix starts the changed
# path decides. (prefix, suites, why); suites: a tuple of suite names or
# patterns, an empty tuple when the file needs no test, None for a file of
# tests/ (its own suite; a helper runs all). A path that no row names runs
# everything, which is always right. PDF Cropper's tests read the
# workflows, the catalogue and the icons too (test_crop.py,
# test_updates_pdf_cropper.py), so no folder is given an empty tuple by guess.
SELECTION = [
    ("tests/", None, "its own suite"),
]
