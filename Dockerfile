FROM condaforge/miniforge3:26.3.2-3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/conda/envs/seller-site/bin:$PATH

WORKDIR /app

COPY environment.yml /app/environment.yml
RUN conda env create --file environment.yml && conda clean --all --yes

RUN useradd --uid 10001 --create-home app
COPY --chown=app:app . /app
RUN DJANGO_SECRET_KEY=build-only \
    DJANGO_ALLOWED_HOSTS=localhost \
    DATABASE_URL=sqlite:////tmp/build.sqlite3 \
    python manage.py collectstatic --noinput

USER app

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
