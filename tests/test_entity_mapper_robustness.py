"""
EntityChunkMapper ignores entity names that only appear in comments / docstrings
and does not match partial identifiers (Python).
"""
from repo_knowledge_graph.entity_chunk_mapper import EntityChunkMapper


def test_entity_in_comments():
    chunks = [
        """
# This function calls calculate_total() to get the sum
def some_function():
    pass
""",
        """
def calculate_total(items):
    '''Calculate the total of all items'''
    return sum(items)
""",
        """
# calculate_total is a useful function
result = calculate_total([1, 2, 3])
""",
    ]
    matches = EntityChunkMapper().find_entity_in_chunks('calculate_total', chunks, entity_type='function')
    # chunk 0 only mentions the name in a comment
    assert matches == {1, 2}


def test_entity_in_docstring():
    chunks = [
        '''
def process_data(data):
    """
    Process data using the transform_value function.
    The transform_value function should be called for each item.
    """
    return [x * 2 for x in data]
''',
        '''
def transform_value(x):
    """Transform a single value"""
    return x * 3
''',
        '''
def main():
    """Main function"""
    result = transform_value(10)
    return result
''',
    ]
    matches = EntityChunkMapper().find_entity_in_chunks('transform_value', chunks, entity_type='function')
    # chunk 0 only mentions the name in a docstring
    assert matches == {1, 2}


def test_partial_name_matching():
    chunks = [
        """
def calculate():
    return 42
""",
        """
def calculate_total():
    # Should not match 'calculate' alone
    return calculate()
""",
        """
# This has calculate_total_sum which should not match 'calculate_total'
calculate_total_sum = 100
""",
    ]
    mapper = EntityChunkMapper()
    assert mapper.find_entity_in_chunks('calculate', chunks, entity_type='function') == {0, 1}
    assert mapper.find_entity_in_chunks('calculate_total', chunks, entity_type='function') == {1}


def test_class_in_comment():
    chunks = [
        """
# The DataProcessor class is defined elsewhere
def helper_function():
    pass
""",
        """
class DataProcessor:
    '''Process data'''
    def process(self):
        pass
""",
        """
# Create a DataProcessor instance
processor = DataProcessor()
""",
    ]
    matches = EntityChunkMapper().find_entity_in_chunks('DataProcessor', chunks, entity_type='class')
    assert matches == {1, 2}


def test_unknown_entity_matches_nothing():
    chunks = ['def foo():\n    return 1\n', '# bar is mentioned here only\n']
    assert EntityChunkMapper().find_entity_in_chunks('bar', chunks, entity_type='function') == set()
