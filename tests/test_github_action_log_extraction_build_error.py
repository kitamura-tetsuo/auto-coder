import pytest

from auto_coder.util.github_action import _extract_error_context


def test_extract_error_context_with_build_error():
    """Verify that build errors are correctly extracted."""
    log_content = ""
    # Add filler lines to simulate a long log where the error might be missed if not prioritized
    for i in range(600):
        log_content += f"2025-10-27T03:26:{i%60:02d}.0000000Z   Filler line {i}\n"

    log_content += """
2025-10-27T03:25:50.0000000Z > my-app@0.0.0 build
2025-10-27T03:25:51.0000000Z > tsc && vite build
2025-10-27T03:25:52.0000000Z 
2025-10-27T03:25:53.0000000Z src/components/App.tsx(10,15): error TS2322: Type 'string' is not assignable to type 'number'.
2025-10-27T03:25:54.0000000Z src/utils/helper.ts(5,10): error TS2304: Cannot find name 'missingVar'.
2025-10-27T03:25:55.0000000Z 
2025-10-27T03:25:56.0000000Z Some other output
2025-10-27T03:25:57.0000000Z more output
"""

    result = _extract_error_context(log_content)

    assert "error TS2322" in result
    assert "error TS2304" in result
    assert "Type 'string' is not assignable to type 'number'" in result


def test_extract_error_context_with_module_not_found():
    """Verify that 'Module not found' errors are extracted."""
    log_content = ""
    # Add filler lines
    for i in range(600):
        log_content += f"2025-10-27T03:26:{i%60:02d}.0000000Z   Filler line {i}\n"

    log_content += """
2025-10-27T03:25:50.0000000Z [vite] connecting...
2025-10-27T03:25:51.0000000Z [vite] connected.
2025-10-27T03:25:52.0000000Z 
2025-10-27T03:25:53.0000000Z Error: Module not found: Error: Can't resolve './missing-component' in '/app/src/components'
2025-10-27T03:25:54.0000000Z     at /app/node_modules/webpack/lib/Compilation.js:2016:28
2025-10-27T03:25:55.0000000Z     at /app/node_modules/webpack/lib/NormalModuleFactory.js:798:13
"""

    result = _extract_error_context(log_content)

    assert "Module not found" in result
    assert "Can't resolve './missing-component'" in result


def test_extract_error_context_with_syntax_error():
    """Verify that SyntaxErrors are extracted."""
    log_content = ""
    # Add filler lines
    for i in range(600):
        log_content += f"2025-10-27T03:26:{i%60:02d}.0000000Z   Filler line {i}\n"

    log_content += """
2025-10-27T03:25:50.0000000Z /app/src/index.js:10
2025-10-27T03:25:51.0000000Z const x = ;
2025-10-27T03:25:52.0000000Z           ^
2025-10-27T03:25:53.0000000Z 
2025-10-27T03:25:54.0000000Z SyntaxError: Unexpected token ';'
2025-10-27T03:25:55.0000000Z     at Object.compileFunction (node:vm:352:18)
2025-10-27T03:25:56.0000000Z     at wrapSafe (node:internal/modules/cjs/loader:1033:15)
"""

    result = _extract_error_context(log_content)

    assert "SyntaxError: Unexpected token ';'" in result
