FROM condaforge/mambaforge:latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# FreeCAD is pinned on purpose.
#
# The previous line was `conda install -c conda-forge freecad python=3.10`,
# which is unpinned and today resolves to FreeCAD 1.1.3 - a different major
# release than the one the generated scripts and the vendored sheetmetal/
# workbench were written against. 1.0.0 matches the FreeCAD the geometry code
# was verified on. Resolves to freecad 1.0.0 py312 + occt 7.8.1 + python
# 3.12.7, and freecadcmd embeds that same interpreter, so there is exactly one
# Python in the image (the explicit python=3.10 is dropped - it would only
# constrain the solve for no benefit).
#
# mamba instead of conda: same packages, much faster solve.
RUN mamba install -y -c conda-forge \
        freecad=1.0.0 \
        wkhtmltopdf \
        networkx \
    && mamba clean -afy

# Redis runs in-container as a fallback; MQTT is a separate container.
RUN apt-get update && apt-get install -y --no-install-recommends \
       build-essential \
       redis-server \
       curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN mkdir -p /app/storage \
    && sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh

EXPOSE 8020

CMD ["/app/entrypoint.sh"]
