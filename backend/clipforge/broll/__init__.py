"""B-roll engine: beats -> queries -> candidates -> windows -> contact sheets -> checks.

Quality over quantity: a slot gets footage only if it passes every check; otherwise it is
marked WEAK ("needs manual cover"). Podcast footage is never used as B-roll.
"""
