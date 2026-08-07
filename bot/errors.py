class DownloadError(RuntimeError):
    pass


class FileTooLargeError(DownloadError):
    pass


class UnsupportedSpotifyContentError(DownloadError):
    pass
