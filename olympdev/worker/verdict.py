from enum import Enum

class Verdict(Enum):
    OK = "OK" # Accepted
    WA = "WA" # Wrong answer
    TL = "TL" # Time limit exceeded
    ML = "ML" # Memory limit exceeded
    RE = "RE" # Runtime error
    CE = "CE" # Compilation error
    FL = "FL" # Testing failed
    PT = "PT" # Partially correct
    CF = "CF" # Check failed