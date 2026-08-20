Here is a command someone ran:

    {STEP}

Rewrite it so it would work for a different project, a different file, a
different name. Replace anything belonging only to this one run with a
placeholder in angle brackets.

Replace: absolute paths, repository names, file names specific to one project,
branch names, ticket numbers, version numbers, host names, people's names.

Keep: the program being run, its flags, its options, and any argument that is
part of how the tool works rather than what it was pointed at.

Examples of the change:

    git commit -am 'fix the export timeout'   ->   git commit -am '<message>'
    pytest tests/export/test_serializer.py    ->   pytest <path to the tests>
    uv run --with pytest pytest tests/        ->   uv run --with pytest pytest <path to the tests>

Write only the rewritten command, on one line, and nothing else.
