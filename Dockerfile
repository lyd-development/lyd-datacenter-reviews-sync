# You can see the Docker images from Apify at https://hub.docker.com/r/apify/.
FROM apify/actor-python-playwright:3.14-1.61.0

USER myuser

COPY --chown=myuser:myuser requirements.txt ./

RUN echo "Python version:" \
 && python --version \
 && echo "Pip version:" \
 && pip --version \
 && echo "Installing dependencies:" \
 && pip install -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

COPY --chown=myuser:myuser . ./

RUN python -m compileall -q google_review_instance/

CMD ["python", "-m", "google_review_instance"]
