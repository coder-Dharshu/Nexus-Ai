"""
Nexus AI — Spotify Tool
Play music, search songs, control playback.
Uses Spotipy (free) + Spotify Web API (free developer account).
"""
from __future__ import annotations
import asyncio
from typing import Optional
import structlog
log = structlog.get_logger(__name__)

class SpotifyTool:
    """
    Controls Spotify via the Web API.
    Requires: Spotify account (free ok) + developer app at developer.spotify.com (free).
    """

    def __init__(self):
        self._sp = None

    def _get_client(self):
        if self._sp: return self._sp
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth
            from src.security.keychain import secrets_manager
            from config.settings import get_settings
            s = get_settings()
            client_id     = secrets_manager.get(s.spotify_client_id_key, required=True)
            client_secret = secrets_manager.get(s.spotify_client_secret_key, required=True)
            self._sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=client_id, client_secret=client_secret,
                redirect_uri=s.spotify_redirect_uri,
                scope="user-modify-playback-state user-read-playback-state user-read-currently-playing streaming",
                open_browser=False,
                cache_path=".spotify_cache",
            ))
            return self._sp
        except ImportError:
            raise RuntimeError("Run: pip install spotipy")
        except Exception as e:
            raise RuntimeError(f"Spotify auth failed: {e}. Run: nexus setup → enter Spotify credentials")

    async def play(self, query: str, device_name: Optional[str] = None) -> dict:
        """
        Play a song, artist, playlist, or album.
        Examples:
          play("Blinding Lights by The Weeknd")
          play("Arijit Singh latest songs")
          play("chill playlist")
          play("AR Rahman hits")
        """
        def _sync_play():
            sp = self._get_client()
            # Search Spotify
            results = sp.search(q=query, limit=5, type="track,playlist,album,artist")
            tracks = results.get("tracks",{}).get("items",[])
            playlists = results.get("playlists",{}).get("items",[])
            artists = results.get("artists",{}).get("items",[])
            albums = results.get("albums",{}).get("items",[])

            # Find best match
            item = None; item_type = None; uri = None
            if tracks:
                item = tracks[0]; item_type = "track"
                uri = item["uri"]
            elif playlists:
                item = playlists[0]; item_type = "playlist"
                uri = item["uri"]
            elif albums:
                item = albums[0]; item_type = "album"
                uri = item["uri"]
            elif artists and artists[0].get("top_tracks"):
                item = artists[0]; item_type = "artist"
                # Get artist top tracks
                top = sp.artist_top_tracks(artists[0]["id"])
                uris = [t["uri"] for t in top.get("tracks",[])[:10]]
                if uris:
                    device_id = self._get_device(sp, device_name)
                    sp.start_playback(device_id=device_id, uris=uris)
                    return {"status":"playing", "type":"artist_top_tracks",
                            "artist":artists[0]["name"], "track_count":len(uris),
                            "message": f"Playing top tracks by {artists[0]['name']}"}

            if not uri:
                return {"status":"error","message":f"No results found for: {query}"}

            device_id = self._get_device(sp, device_name)
            if item_type == "track":
                sp.start_playback(device_id=device_id, uris=[uri])
                artists_str = ", ".join(a["name"] for a in item.get("artists",[]))
                return {"status":"playing","type":"track","track":item["name"],
                        "artist":artists_str,"album":item.get("album",{}).get("name",""),
                        "duration_ms":item.get("duration_ms",0),
                        "message":f"Now playing: {item['name']} by {artists_str}"}
            else:
                sp.start_playback(device_id=device_id, context_uri=uri)
                return {"status":"playing","type":item_type,"name":item.get("name",""),
                        "message":f"Playing {item_type}: {item.get('name','')}"}

        try:
            return await asyncio.to_thread(_sync_play)
        except Exception as e:
            log.error("spotify_play_error", error=str(e))
            return {"status":"error","message":str(e)}

    async def pause(self) -> dict:
        def _sync():
            sp = self._get_client()
            sp.pause_playback()
            return {"status":"paused","message":"Playback paused"}
        try: return await asyncio.to_thread(_sync)
        except Exception as e: return {"status":"error","message":str(e)}

    async def resume(self) -> dict:
        def _sync():
            sp = self._get_client()
            sp.start_playback()
            return {"status":"playing","message":"Playback resumed"}
        try: return await asyncio.to_thread(_sync)
        except Exception as e: return {"status":"error","message":str(e)}

    async def next_track(self) -> dict:
        def _sync():
            sp = self._get_client()
            sp.next_track()
            import time; time.sleep(0.5)
            cur = sp.currently_playing()
            if cur and cur.get("item"):
                item = cur["item"]
                artists = ", ".join(a["name"] for a in item.get("artists",[]))
                return {"status":"playing","track":item["name"],"artist":artists,
                        "message":f"Skipped to: {item['name']} by {artists}"}
            return {"status":"playing","message":"Skipped to next track"}
        try: return await asyncio.to_thread(_sync)
        except Exception as e: return {"status":"error","message":str(e)}

    async def set_volume(self, volume_pct: int) -> dict:
        vol = max(0, min(100, volume_pct))
        def _sync():
            sp = self._get_client()
            sp.volume(vol)
            return {"status":"ok","volume":vol,"message":f"Volume set to {vol}%"}
        try: return await asyncio.to_thread(_sync)
        except Exception as e: return {"status":"error","message":str(e)}

    async def get_current(self) -> dict:
        def _sync():
            sp = self._get_client()
            cur = sp.currently_playing()
            if not cur or not cur.get("item"):
                return {"status":"stopped","message":"Nothing is playing"}
            item = cur["item"]
            artists = ", ".join(a["name"] for a in item.get("artists",[]))
            progress = cur.get("progress_ms",0)
            duration = item.get("duration_ms",1)
            pct = round(progress/duration*100)
            return {"status":"playing","track":item["name"],"artist":artists,
                    "album":item.get("album",{}).get("name",""),
                    "progress_pct":pct,"is_playing":cur.get("is_playing",False),
                    "message":f"Currently playing: {item['name']} by {artists} ({pct}%)"}
        try: return await asyncio.to_thread(_sync)
        except Exception as e: return {"status":"error","message":str(e)}

    async def search(self, query: str, limit: int = 5) -> dict:
        def _sync():
            sp = self._get_client()
            r = sp.search(q=query, limit=limit, type="track")
            tracks = r.get("tracks",{}).get("items",[])
            return {"results":[{
                "track": t["name"],
                "artist": ", ".join(a["name"] for a in t.get("artists",[])),
                "album": t.get("album",{}).get("name",""),
                "duration_s": t.get("duration_ms",0)//1000,
                "uri": t["uri"],
            } for t in tracks], "count": len(tracks)}
        try: return await asyncio.to_thread(_sync)
        except Exception as e: return {"status":"error","message":str(e)}

    @staticmethod
    def _get_device(sp, preferred_name: Optional[str] = None):
        devices = sp.devices().get("devices",[])
        if not devices: return None
        if preferred_name:
            for d in devices:
                if preferred_name.lower() in d.get("name","").lower():
                    return d["id"]
        # Return first active device
        for d in devices:
            if d.get("is_active"): return d["id"]
        return devices[0]["id"] if devices else None

spotify_tool = SpotifyTool()
