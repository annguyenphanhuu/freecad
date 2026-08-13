"""
Production-Ready Script Pre-processor for FreeCAD Python Scripts
Auto mode: AST (Python 3.9+) → Line Parsing fallback
Zero external dependencies
"""

import ast
import sys
import os
from typing import Tuple, List


class PreprocessorResult:
    """Result object with detailed status"""
    def __init__(self):
        self.success = True
        self.warnings: List[str] = []
        self.errors: List[str] = []
        self.changes: List[str] = []
        self.method_used: str = ""
    
    def add_warning(self, msg: str):
        self.warnings.append(msg)
    
    def add_error(self, msg: str):
        self.errors.append(msg)
        self.success = False
    
    def add_change(self, msg: str):
        self.changes.append(msg)


class ScriptTransformer(ast.NodeTransformer):
    """
    AST NodeTransformer - Modifies Python AST directly
    Works with ANY formatting, whitespace, comments
    """
    
    def __init__(self, user_id: str, output_dir: str):
        self.user_id = user_id
        self.output_dir = output_dir
        self.changes = []
        self.found_sanitized_title = False
        self.found_output_dir = False
        self.original_title = None
    
    def visit_Assign(self, node):
        """Visit assignment nodes: var = value"""
        
        # Check if this is sanitized_title assignment
        if (len(node.targets) == 1 and 
            isinstance(node.targets[0], ast.Name) and 
            node.targets[0].id == 'sanitized_title'):
            
            # Get original value
            if isinstance(node.value, ast.Constant):
                self.original_title = str(node.value.value)
            elif hasattr(ast, 'Str') and isinstance(node.value, ast.Str):  # Python 3.7 compat
                self.original_title = node.value.s
            else:
                self.original_title = "unknown"
            
            # Replace with new value
            if sys.version_info >= (3, 8):
                node.value = ast.Constant(value=self.user_id)
            else:
                node.value = ast.Str(s=self.user_id)
            
            self.found_sanitized_title = True
            self.changes.append(f"sanitized_title: '{self.original_title}' → '{self.user_id}'")
        
        # Check if this is output_dir_abs assignment
        elif (len(node.targets) == 1 and 
              isinstance(node.targets[0], ast.Name) and 
              node.targets[0].id == 'output_dir_abs'):
            
            # Replace entire assignment with simple string
            if sys.version_info >= (3, 8):
                node.value = ast.Constant(value=self.output_dir)
            else:
                node.value = ast.Str(s=self.output_dir)
            
            self.found_output_dir = True
            self.changes.append(f"output_dir_abs → {self.output_dir}")
        
        return node


def preprocess_with_ast(script_content: str, user_id: str, output_dir: str) -> Tuple[str, PreprocessorResult]:
    """
    Method 1: AST-based preprocessing (MOST ROBUST)
    Requires Python 3.9+ for ast.unparse()
    """
    
    result = PreprocessorResult()
    result.method_used = "AST"
    
    # Check Python version
    if sys.version_info < (3, 9):
        result.add_error("AST method requires Python 3.9+ (for ast.unparse)")
        return script_content, result
    
    try:
        # Parse script into AST
        tree = ast.parse(script_content)
        
        # Transform AST
        transformer = ScriptTransformer(user_id, output_dir)
        new_tree = transformer.visit(tree)
        
        # Fix missing locations (required after AST modification)
        ast.fix_missing_locations(new_tree)
        
        # Convert AST back to source code (Python 3.9+)
        modified_script = ast.unparse(new_tree)
        
        # Add header
        header = f'''# ============================================================
# PRE-PROCESSED by FreeCAD Worker (AST Method)
# User ID: {user_id}
# Output Directory: {output_dir}
# Expected outputs: {user_id}.step, {user_id}.obj
# ============================================================

'''
        modified_script = header + modified_script
        
        # Record changes
        for change in transformer.changes:
            result.add_change(change)
        
        # Validation
        if not transformer.found_sanitized_title:
            result.add_warning("sanitized_title variable not found in AST")
        if not transformer.found_output_dir:
            result.add_warning("output_dir_abs variable not found in AST")
        
        return modified_script, result
        
    except SyntaxError as e:
        result.add_error(f"Script has syntax errors: {e}")
        return script_content, result
    except Exception as e:
        result.add_error(f"AST processing failed: {e}")
        return script_content, result


def preprocess_with_line_parsing(script_content: str, user_id: str, output_dir: str) -> Tuple[str, PreprocessorResult]:
    """
    Method 2: Line-by-line parsing (FAST & RELIABLE FALLBACK)
    Works on any Python version
    """
    
    result = PreprocessorResult()
    result.method_used = "Line Parsing"
    
    lines = script_content.split('\n')
    modified_lines = []
    found_sanitized_title = False
    found_output_dir = False
    skip_next_lines = 0
    original_title = None
    
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        
        # Skip lines if we're in multiline continuation
        if skip_next_lines > 0:
            skip_next_lines -= 1
            i += 1
            continue
        
        # Detect sanitized_title assignment
        if 'sanitized_title' in stripped and '=' in stripped and not stripped.startswith('#'):
            # Extract indentation
            indent = line[:len(line) - len(line.lstrip())]
            
            # Try to extract original value
            try:
                if '"' in stripped:
                    original_title = stripped.split('"')[1]
                elif "'" in stripped:
                    original_title = stripped.split("'")[1]
            except:
                original_title = "unknown"
            
            # Preserve comment if exists
            if '#' in stripped:
                comment_part = ' #' + stripped.split('#', 1)[1]
                new_line = f'{indent}sanitized_title = "{user_id}"{comment_part}'
            else:
                new_line = f'{indent}sanitized_title = "{user_id}"'
            
            modified_lines.append(new_line)
            found_sanitized_title = True
            result.add_change(f"sanitized_title: '{original_title}' → '{user_id}'")
            i += 1
            continue
        
        # Detect output_dir_abs assignment
        if 'output_dir_abs' in stripped and '=' in stripped and not stripped.startswith('#'):
            indent = line[:len(line) - len(line.lstrip())]
            new_line = f'{indent}output_dir_abs = "{output_dir}"'
            modified_lines.append(new_line)
            found_output_dir = True
            result.add_change(f"output_dir_abs → {output_dir}")
            
            # Check if this is multiline assignment
            if '(' in stripped and ')' not in stripped:
                # Count lines until closing parenthesis
                j = i + 1
                while j < len(lines) and ')' not in lines[j]:
                    j += 1
                skip_next_lines = j - i  # Skip all continuation lines
            
            i += 1
            continue
        
        # Keep original line
        modified_lines.append(line)
        i += 1
    
    # Validation
    if not found_sanitized_title:
        result.add_warning("sanitized_title variable not found")
    if not found_output_dir:
        result.add_warning("output_dir_abs variable not found")
    
    modified_script = '\n'.join(modified_lines)
    
    # Add header
    header = f'''# ============================================================
# PRE-PROCESSED by FreeCAD Worker (Line Parsing Method)
# User ID: {user_id}
# Output Directory: {output_dir}
# Expected outputs: {user_id}.step, {user_id}.obj
# ============================================================

'''
    modified_script = header + modified_script
    
    return modified_script, result


def preprocess_freecad_script(script_content: str, user_id: str, output_dir: str) -> Tuple[str, PreprocessorResult]:
    """
    Main preprocessing function with AUTO mode.
    
    Strategy:
    1. Try AST method (Python 3.9+) - most robust
    2. If AST fails or Python < 3.9, use Line Parsing - fast & reliable
    
    Returns:
        (modified_script, result_object)
    """
    
    # Try AST first if Python 3.9+
    if sys.version_info >= (3, 9):
        try:
            result_script, result_obj = preprocess_with_ast(script_content, user_id, output_dir)
            
            # Check if AST succeeded and found both variables
            if result_obj.success and not result_obj.errors:
                # Verify changes were made
                if len(result_obj.changes) >= 2:
                    return result_script, result_obj
                elif len(result_obj.changes) > 0:
                    # AST found some but not all - still use it
                    return result_script, result_obj
        except Exception as e:
            # AST failed, will fall back to line parsing
            print(f"[Pre-processor] AST method failed: {e}, falling back to line parsing")
    
    # Fallback to line parsing
    return preprocess_with_line_parsing(script_content, user_id, output_dir)


def save_preprocessed_script(original_path: str, user_id: str, output_dir: str) -> Tuple[str, PreprocessorResult]:
    """
    Read, preprocess, and save script with AUTO method selection.
    
    Args:
        original_path: Path to original script
        user_id: User ID for file naming
        output_dir: Target output directory
    
    Returns:
        (path_to_saved_script, result_object)
    """
    
    result = PreprocessorResult()
    
    print("=" * 70)
    print(f"[Pre-processor] Starting script preprocessing (AUTO mode)")
    print(f"[Pre-processor] Python version: {sys.version_info.major}.{sys.version_info.minor}")
    print(f"[Pre-processor] User ID: {user_id}")
    print(f"[Pre-processor] Script: {original_path}")
    print(f"[Pre-processor] Output Dir: {output_dir}")
    print("=" * 70)
    
    # Read original script
    try:
        with open(original_path, 'r', encoding='utf-8') as f:
            original_content = f.read()
        print(f"[Pre-processor] ✓ Read {len(original_content)} bytes")
    except Exception as e:
        result.add_error(f"Failed to read script: {e}")
        print(f"[Pre-processor] ✗ {result.errors[-1]}")
        return original_path, result
    
    # Preprocess
    modified_content, preprocess_result = preprocess_freecad_script(
        original_content, user_id, output_dir
    )
    
    # Merge results
    result.warnings.extend(preprocess_result.warnings)
    result.errors.extend(preprocess_result.errors)
    result.changes.extend(preprocess_result.changes)
    result.success = preprocess_result.success
    result.method_used = preprocess_result.method_used
    
    print(f"[Pre-processor] Method used: {result.method_used}")
    
    # Save preprocessed script
    if result.success or len(result.changes) > 0:
        try:
            with open(original_path, 'w', encoding='utf-8') as f:
                f.write(modified_content)
            print(f"[Pre-processor] ✓ Saved {len(modified_content)} bytes")
        except Exception as e:
            result.add_error(f"Failed to save preprocessed script: {e}")
            print(f"[Pre-processor] ✗ {result.errors[-1]}")
    
    # Print detailed results
    if result.changes:
        print("\n" + "=" * 70)
        print("[Pre-processor] CHANGES MADE:")
        print("=" * 70)
        for change in result.changes:
            print(f"  ✓ {change}")
    
    if result.warnings:
        print("\n" + "=" * 70)
        print("[Pre-processor] WARNINGS:")
        print("=" * 70)
        for warning in result.warnings:
            print(f"  ⚠ {warning}")
    
    if result.errors:
        print("\n" + "=" * 70)
        print("[Pre-processor] ERRORS:")
        print("=" * 70)
        for error in result.errors:
            print(f"  ✗ {error}")
    
    print("=" * 70)
    print(f"[Pre-processor] Status: {'✓ SUCCESS' if result.success else '✗ FAILED'}")
    print(f"[Pre-processor] Changes: {len(result.changes)} | Warnings: {len(result.warnings)} | Errors: {len(result.errors)}")
    print("=" * 70)
    
    return original_path, result


# ===== TEST & VALIDATION =====
def test_preprocessor():
    """Comprehensive test with all sample scripts"""
    
    test_cases = [
        {
            "name": "Standard format (Document 2)",
            "script": '''sanitized_title = "Cache_Ventilateur_Rect_500x300x1_Alu"   # DO NOT CHANGE THIS VALUE
output_dir_abs = os.path.abspath(os.path.join(script_dir, "..", "cad_outputs_generated"))
step_path = os.path.join(output_dir_abs, f"{sanitized_title}.step")'''
        },
        {
            "name": "Multiline format (Document 4)",
            "script": '''sanitized_title = "session_bb2f2a_889837"  # DO NOT CHANGE THIS VALUE
output_dir_abs = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'cad_outputs_generated')
)
step_filename_abs = os.path.join(output_dir_abs, f"{sanitized_title}.step")'''
        },
        {
            "name": "No spaces",
            "script": '''sanitized_title="Perforated_sheet_R12_T16_200x200x2"
output_dir_abs=os.path.abspath(os.path.join(script_dir,"..","..", "cad_outputs_generated"))'''
        },
        {
            "name": "Mixed quotes",
            "script": '''sanitized_title = 'test_model_123'  # comment
output_dir_abs = os.path.abspath(os.path.join(script_dir, "..", "cad_outputs_generated"))'''
        },
    ]
    
    print("\n" + "=" * 70)
    print("COMPREHENSIVE TESTING")
    print("=" * 70)
    print(f"Python version: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    print("=" * 70)
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n{'='*70}")
        print(f"TEST CASE {i}: {test_case['name']}")
        print('='*70)
        
        result_script, result_obj = preprocess_freecad_script(
            test_case['script'],
            "user_test_123",
            "/app/storage/user_test_123/output"
        )
        
        # Print results
        print(f"\nMethod: {result_obj.method_used}")
        print(f"Status: {'✓ SUCCESS' if result_obj.success else '✗ FAILED'}")
        print(f"Changes: {len(result_obj.changes)}")
        
        for change in result_obj.changes:
            print(f"  ✓ {change}")
        for warning in result_obj.warnings:
            print(f"  ⚠ {warning}")
        for error in result_obj.errors:
            print(f"  ✗ {error}")
        
        # Verify output
        print("\nVERIFICATION:")
        if 'sanitized_title = "user_test_123"' in result_script:
            print("  ✓ sanitized_title correctly replaced")
        else:
            print("  ✗ sanitized_title NOT replaced")
        
        if '"/app/storage/user_test_123/output"' in result_script:
            print("  ✓ output_dir_abs correctly replaced")
        else:
            print("  ✗ output_dir_abs NOT replaced")
    
    print("\n" + "=" * 70)
    print("TESTING COMPLETED")
    print("=" * 70)


def validate_with_real_file(file_path: str):
    """Test with actual file from your documents"""
    
    print("\n" + "=" * 70)
    print("REAL FILE VALIDATION")
    print("=" * 70)
    
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return
    
    # Create temp copy
    import shutil
    temp_path = file_path + ".test_copy"
    shutil.copy(file_path, temp_path)
    
    try:
        # Process
        result_path, result_obj = save_preprocessed_script(
            temp_path,
            "user_validation_123",
            "/app/storage/user_validation_123/output"
        )
        
        # Show processed content
        print("\n" + "=" * 70)
        print("PROCESSED SCRIPT (first 30 lines):")
        print("=" * 70)
        with open(result_path, 'r') as f:
            lines = f.readlines()[:30]
            for i, line in enumerate(lines, 1):
                print(f"{i:3d} | {line.rstrip()}")
        
    finally:
        # Cleanup
        if os.path.exists(temp_path):
            os.remove(temp_path)


if __name__ == "__main__":
    # Run tests
    test_preprocessor()
    
    # Uncomment to test with your actual files:
    # validate_with_real_file("box_unknown_20251023_544f02.py")
    # validate_with_real_file("box_unknown_20251110_fb033d.py")