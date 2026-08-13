"""
DFM Shape Chatbot - A chatbot for drawing geometric shapes using natural language.
"""

__version__ = "0.1.0"

# Configure FAISS for CPU-only usage before any other imports
import os
import logging

# Set environment variables for FAISS CPU-only mode
os.environ['FAISS_DISABLE_GPU'] = '1'
os.environ['CUDA_VISIBLE_DEVICES'] = ''
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'
os.environ['OMP_NUM_THREADS'] = '1'

# Suppress FAISS GPU warnings
logging.getLogger('faiss').setLevel(logging.ERROR)
