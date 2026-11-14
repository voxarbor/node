# Data-only image: the Opus-MT tc-big models converted to CTranslate2 int8 by services/translate/convert_opus.sh
# (build context = a directory with the converted opus-mt/ tree). A worker node's `init` copies them into the
# translate cache volume once; without them translate falls back to the slower NLLB.
FROM busybox:1.37.0
COPY opus-mt /models/opus-mt
CMD ["sh", "-c", "if [ -f /cache/opus-mt/.complete ]; then echo 'opus models present'; else mkdir -p /cache/opus-mt && cp -r /models/opus-mt/. /cache/opus-mt/ && chown -R 65534:65534 /cache/opus-mt && touch /cache/opus-mt/.complete && echo 'opus models installed'; fi"]
