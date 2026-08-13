#!/usr/bin/env python3
"""
Technical Drawing Generator
Generates technical drawings (SVG + PDF) from STEP files using FreeCAD
Based on: https://github.com/annguyenphanhuu/techdraw.git
"""

import os
import sys
import subprocess
import tempfile
import logging
from pathlib import Path
from typing import Tuple, Optional, Dict, Any
from datetime import datetime

# Setup logging
logger = logging.getLogger(__name__)

class TechnicalDrawingGenerator:
    """
    Generates technical drawings from STEP files using the cloned techdraw repository
    """

    def __init__(self):
        """Initialize the technical drawing generator"""
        self.script_dir = Path(__file__).parent
        self.techdraw_dir = self.script_dir / "techdraw"
        self.templates_dir = self.techdraw_dir / "templates"
        self.temp_output_dir = self.techdraw_dir / "temp_output"
        self.base_script_path = self.techdraw_dir / "run_techdraw_final.py"
        self.template_name = "A4_TOLERY.svg"

        # Ensure directories exist
        self.temp_output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Technical drawing directory: {self.techdraw_dir}")
        logger.info(f"Templates directory: {self.templates_dir}")
        logger.info(f"Base script: {self.base_script_path}")

        # Validate setup
        self._validate_setup()

    def _validate_setup(self):
        """Validate that all required files exist"""
        if not self.base_script_path.exists():
            raise FileNotFoundError(f"Base script not found: {self.base_script_path}")

        template_path = self.templates_dir / self.template_name
        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

        logger.info("Technical drawing setup validated successfully")

    
    def generate_technical_drawing(
        self, 
        step_file_path: Path, 
        output_dir: Path,
        base_filename: str = None
    ) -> Tuple[bool, Optional[Path], Optional[Path], str]:
        """
        Generate technical drawing from STEP file
        
        Args:
            step_file_path: Path to input STEP file
            output_dir: Directory to save output files
            base_filename: Base name for output files (without extension)
            
        Returns:
            Tuple of (success, svg_path, pdf_path, message)
        """
        try:
            # Validate inputs
            if not step_file_path.exists():
                return False, None, None, f"STEP file not found: {step_file_path}"
            
            # Create output directory
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate filenames
            if base_filename is None:
                base_filename = step_file_path.stem + "_technical"
            
            svg_output_path = output_dir / f"{base_filename}.svg"
            
            # Generate SVG using FreeCAD
            success, svg_path, message = self._generate_svg_with_freecad(
                step_file_path, svg_output_path
            )
            
            if not success:
                return False, None, None, message
            
            # Convert SVG to PDF
            pdf_success, pdf_path, pdf_message = self._convert_svg_to_pdf(svg_path)
            
            if pdf_success:
                return True, svg_path, pdf_path, "Technical drawing generated successfully"
            else:
                return True, svg_path, None, f"SVG generated but PDF conversion failed: {pdf_message}"
                
        except Exception as e:
            logger.error(f"Error generating technical drawing: {e}")
            return False, None, None, f"Technical drawing generation failed: {str(e)}"
    
    def _generate_svg_with_freecad(
        self, 
        step_file_path: Path, 
        svg_output_path: Path
    ) -> Tuple[bool, Optional[Path], str]:
        """Generate SVG using FreeCAD script"""
        
        logger.info(f"Starting SVG generation for STEP file: {step_file_path}")
        logger.info(f"Target SVG output path: {svg_output_path}")
        
        # Create modified script content
        script_content = self._create_modified_script(step_file_path, svg_output_path)
        
        # Write script to temporary file
        try:
            logger.info("Creating temporary script file...")
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
                f.write(script_content)
                script_path = f.name
            
            # Execute FreeCAD script
            logger.info(f"Executing FreeCAD script: {script_path}")
            result = subprocess.run(
                ["freecadcmd", script_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=120
            )
            
            # Clean up script file
            try:
                os.unlink(script_path)
            except:
                pass
            
            # Check results
            if result.returncode == 0 and svg_output_path.exists():
                logger.info(f"SVG generated successfully: {svg_output_path}")
                return True, svg_output_path, "SVG generation completed"
            else:
                logger.error(f"FreeCAD script execution failed:")
                logger.error(f"Return code: {result.returncode}")
                logger.error(f"STDOUT: {result.stdout}")
                logger.error(f"STDERR: {result.stderr}")
                logger.error(f"SVG output path exists: {svg_output_path.exists()}")
                
                return False, None, f"FreeCAD execution failed: {result.stdout}"
                
        except subprocess.TimeoutExpired:
            return False, None, "FreeCAD script execution timeout"
        except Exception as e:
            logger.error(f"Error executing FreeCAD script: {e}")
            return False, None, f"Script execution error: {str(e)}"
    
    def _convert_svg_to_pdf(self, svg_path: Path) -> Tuple[bool, Optional[Path], str]:
        """Convert SVG to PDF using cairosvg"""
        try:
            import cairosvg
            
            pdf_path = svg_path.with_suffix('.pdf')
            
            logger.info(f"Converting SVG to PDF: {svg_path} -> {pdf_path}")
            
            cairosvg.svg2pdf(url=str(svg_path), write_to=str(pdf_path))
            
            if pdf_path.exists():
                logger.info(f"PDF generated successfully: {pdf_path}")
                return True, pdf_path, "PDF conversion completed"
            else:
                return False, None, "PDF file was not created"
                
        except ImportError:
            logger.warning("cairosvg not available, trying alternative PDF conversion")
            return self._convert_svg_to_pdf_alternative(svg_path)
        except Exception as e:
            logger.error(f"Error in PDF conversion: {e}")
            return False, None, f"PDF conversion error: {str(e)}"
    
    def _convert_svg_to_pdf_alternative(self, svg_path: Path) -> Tuple[bool, Optional[Path], str]:
        """Alternative PDF conversion using wkhtmltopdf or other tools"""
        try:
            pdf_path = svg_path.with_suffix('.pdf')
            
            # Try wkhtmltopdf
            result = subprocess.run([
                "wkhtmltopdf", 
                "--page-size", "A4",
                "--orientation", "Landscape",
                str(svg_path), 
                str(pdf_path)
            ], capture_output=True, text=True, timeout=60)
            
            if result.returncode == 0 and pdf_path.exists():
                logger.info(f"PDF generated using wkhtmltopdf: {pdf_path}")
                return True, pdf_path, "PDF conversion completed with wkhtmltopdf"
            else:
                logger.warning("wkhtmltopdf failed, PDF conversion not available")
                return False, None, "PDF conversion tools not available"
                
        except FileNotFoundError:
            logger.warning("wkhtmltopdf not found, PDF conversion not available")
            return False, None, "PDF conversion tools not available"
        except Exception as e:
            logger.error(f"Error in alternative PDF conversion: {e}")
            return False, None, f"PDF conversion error: {str(e)}"

    def _create_modified_script(self, step_file_path: Path, svg_output_path: Path) -> str:
        """Create modified FreeCAD script with dynamic paths"""

        # Read the base script
        with open(self.base_script_path, 'r', encoding='utf-8') as f:
            base_script = f.read()

        # Get absolute paths
        step_file_str = str(step_file_path.absolute()).replace('\\', '/')
        svg_output_str = str(svg_output_path.absolute()).replace('\\', '/')
        template_str = str((self.templates_dir / self.template_name).absolute()).replace('\\', '/')

        # Replace the configuration section
        config_replacement = f'''# --- Configuration ---
script_dir = os.path.dirname(os.path.abspath(__file__))
step_file_name = "sheet.step"
output_svg_name = "output.svg"
template_name = "A4_TOLERY.svg"

STEP_FILE_PATH = r"{step_file_str}"
OUTPUT_SVG_PATH = r"{svg_output_str}"
TEMPLATE_PATH = r"{template_str}"'''

        # Find and replace the configuration section
        import re

        # Pattern to match the configuration section
        config_pattern = r'# --- Configuration ---.*?TEMPLATE_PATH = os\.path\.join\(script_dir, "templates", template_name\)'

        modified_script = re.sub(
            config_pattern,
            config_replacement,
            base_script,
            flags=re.DOTALL
        )

        return modified_script


def generate_technical_drawing_from_step(
    step_file_path: Path,
    output_dir: Path,
    base_filename: str = None
) -> Dict[str, Any]:
    """
    Standalone function to generate technical drawing from STEP file

    Args:
        step_file_path: Path to input STEP file
        output_dir: Directory to save output files
        base_filename: Base name for output files

    Returns:
        Dictionary with generation results
    """
    try:
        generator = TechnicalDrawingGenerator()
        success, svg_path, pdf_path, message = generator.generate_technical_drawing(
            step_file_path, output_dir, base_filename
        )

        return {
            "success": success,
            "svg_path": str(svg_path) if svg_path else None,
            "pdf_path": str(pdf_path) if pdf_path else None,
            "message": message,
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Error in standalone technical drawing generation: {e}")
        return {
            "success": False,
            "svg_path": None,
            "pdf_path": None,
            "message": f"Generation failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }
