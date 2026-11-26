from repo_knowledge_graph.entity_extractor import HTMLEntityExtractor


def test_html_entity_extraction():
    """Test HTML entity extraction with JavaScript."""
    # Example hybrid integration
    html_code = """
<html>
  <body>
    <button id="submitBtn" onclick="submitForm('userForm')">Submit</button>
    <script>
      function submitForm(id) {
        console.log(id);
      }
    </script>
  </body>
</html>
"""

    html_extractor = HTMLEntityExtractor()
    declared, called = html_extractor.extract_entities(html_code)


    # Assertions for pytest
    assert declared is not None, "Declared entities should not be None"
    assert called is not None, "Called entities should not be None"
