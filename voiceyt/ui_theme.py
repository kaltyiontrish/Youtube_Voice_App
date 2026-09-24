"""Built-in semantic color themes for the desktop UI."""
from __future__ import annotations

from dataclasses import dataclass

THEMES = {
    "midnight": {"background": "#101216", "surface": "#181b20", "surface_alt": "#20242a", "surface_raised": "#272c33", "hover": "#303840", "text": "#f4f6f8", "text_dim": "#9aa1aa", "accent": "#1db954", "accent_dim": "#168a43", "danger": "#f05d5e", "warning": "#f4b740", "success": "#1db954", "selection": "#294d38", "focus": "#1db954", "border": "#39414b"},
    "ocean": {"background": "#0d141a", "surface": "#131e26", "surface_alt": "#1b2a35", "surface_raised": "#243845", "hover": "#2c4655", "text": "#eef8fb", "text_dim": "#9bb2bf", "accent": "#35b8d4", "accent_dim": "#238ba3", "danger": "#f06b73", "warning": "#f0b866", "success": "#54c98a", "selection": "#1b4b59", "focus": "#35b8d4", "border": "#355362"},
    "ember": {"background": "#17110f", "surface": "#211815", "surface_alt": "#2c201c", "surface_raised": "#3b2922", "hover": "#493127", "text": "#fff4ed", "text_dim": "#c5a99b", "accent": "#f08a4b", "accent_dim": "#bd6334", "danger": "#f06b6b", "warning": "#f0c060", "success": "#7acb86", "selection": "#5a3525", "focus": "#f08a4b", "border": "#563a30"},
    "mono": {"background": "#111111", "surface": "#1b1b1b", "surface_alt": "#292929", "surface_raised": "#383838", "hover": "#4b4b4b", "text": "#f0f0f0", "text_dim": "#b8b8b8", "accent": "#f0f0f0", "accent_dim": "#b8b8b8", "danger": "#ff6b6b", "warning": "#f0c060", "success": "#d0d0d0", "selection": "#4a4a4a", "focus": "#ffffff", "border": "#555555"},
    "rose": {"background": "#171116", "surface": "#211820", "surface_alt": "#2e202b", "surface_raised": "#402a3a", "hover": "#50364a", "text": "#fff0f7", "text_dim": "#c6a7b8", "accent": "#e56aa7", "accent_dim": "#b94d83", "danger": "#ff6b82", "warning": "#f0c060", "success": "#d58be0", "selection": "#5c2e4a", "focus": "#e56aa7", "border": "#59384c"},
}

def get_theme(name: str) -> dict[str, str]:
    return dict(THEMES.get(name, THEMES["midnight"]))

def theme_names() -> tuple[str, ...]:
    return tuple(THEMES)
