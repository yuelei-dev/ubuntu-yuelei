"""Capability registry assembled from domain modules."""

from . import audio, breakdown, image, kuaijian, leads, script_to_video, text, video


HANDLERS = {}
for domain in (image, text, leads, audio, video, breakdown, script_to_video, kuaijian):
    HANDLERS.update(domain.HANDLERS)
