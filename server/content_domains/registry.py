"""Capability registry assembled from domain modules."""

import importlib

from . import (
    audio, breakdown, canvas_agent, image, leads, script_to_video,
    short_drama_assembly_render, short_drama_playback_render,
    short_drama_sound_effect, text, video,
)


REQUIRED_DOMAINS = (
    image, text, canvas_agent, leads, audio, video, breakdown, script_to_video,
    short_drama_assembly_render, short_drama_playback_render,
    short_drama_sound_effect,
)


def build_handlers(optional_importer=None, warning=None):
    handlers = {}
    for domain in REQUIRED_DOMAINS:
        handlers.update(domain.HANDLERS)
    importer = optional_importer or (
        lambda name: importlib.import_module("." + name, __package__)
    )
    try:
        optional = importer("director_agent")
        handlers.update(optional.HANDLERS)
    except Exception as error:
        message = "[registry] optional director_agent unavailable: %s" % error
        if warning is not None:
            warning(message)
        else:
            print(message, flush=True)
    return handlers


HANDLERS = build_handlers()
