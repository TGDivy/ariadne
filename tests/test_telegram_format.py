from ariadne.telegram.format import render_telegram_html


def test_markdown_is_rendered_as_safe_telegram_html() -> None:
    rendered = render_telegram_html(
        "## Direction\n\n"
        "**C++** & <vector> with `std::vector` and "
        "[docs](https://example.com).\n\n"
        "- First step\n"
        "- Second step"
    )

    assert rendered == (
        "<b>Direction</b>\n\n"
        "<b>C++</b> &amp; &lt;vector&gt; with <code>std::vector</code> and "
        '<a href="https://example.com">docs</a>.\n\n'
        "• First step\n"
        "• Second step"
    )


def test_fenced_code_blocks_preserve_the_language_and_escape_contents() -> None:
    rendered = render_telegram_html("```cpp\nstd::vector<int> values;\n```")

    assert rendered == (
        '<pre><code class="language-cpp">std::vector&lt;int&gt; values;\n</code></pre>'
    )


def test_ordered_lists_keep_their_markdown_numbering() -> None:
    rendered = render_telegram_html("3. Research\n4. Practice")

    assert rendered == "3. Research\n4. Practice"
