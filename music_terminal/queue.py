# -*- coding: utf-8 -*-
"""Cola de reproducción (equivalente a las queues del MusicService de la app)."""
import random


class Queue:
    def __init__(self):
        self.items = []          # [{'id','title','artists','album','duration',…}, …]
        self.index = -1
        self.mode = "none"       # none | all | one
        self.shuffle = False
        self._order = []         # orden real cuando hay shuffle
        self.context = None      # {'title': …}

    def __len__(self):
        return len(self.items)

    def current(self):
        if 0 <= self.index < len(self.items):
            return self.items[self.index]
        return None

    def set_items(self, items, start=0, context=None):
        self.items = list(items)
        self.index = start if 0 <= start < len(items) else -1
        self.context = context
        if self.shuffle:
            self._reshuffle(keep=self.index)
        else:
            self._order = list(range(len(self.items)))

    def add(self, item, after_current=False):
        self.items.append(item)
        if self.shuffle:
            self._reshuffle(keep=self.index)
        elif after_current:
            pos = self.index + 1
            self.items = (self.items[:pos] + [self.items.pop()] +
                          self.items[pos:])
            self._order = None
        self._rebuild_order()

    def _rebuild_order(self):
        if self.shuffle:
            self._order = self._shuffled()
        else:
            self._order = list(range(len(self.items)))

    def _shuffled(self, first=None):
        base = [i for i in range(len(self.items)) if i != first]
        rnd = random.Random(random.randint(0, 1 << 30))
        rnd.shuffle(base)
        return ([first] if first is not None else []) + base

    def _reshuffle(self, keep=None):
        self._order = self._shuffled(first=keep if keep is not None else None)

    def toggle_shuffle(self):
        self.shuffle = not self.shuffle
        self._rebuild_order()
        return self.shuffle

    def cycle_repeat(self):
        self.mode = {"none": "all", "all": "one", "one": "none"}[self.mode]
        return self.mode

    # ------------------------------------------------------------------ #
    def next_index(self, only_forward=False):
        """Índice del siguiente a reproducir o None."""
        if not self._order:
            self._rebuild_order()
        if not self._order:
            return None
        if self.mode == "one" and not only_forward and self.index >= 0:
            return self.index
        try:
            pos = self._order.index(self.index)
        except ValueError:
            pos = -1
        if pos + 1 < len(self._order):
            return self._order[pos + 1]
        if self.mode == "all" or self.shuffle:
            return self._order[0] if self._order else None
        return None

    def prev_index(self):
        if not self._order:
            return None
        try:
            pos = self._order.index(self.index)
        except ValueError:
            return self._order[0] if self._order else None
        if pos > 0:
            return self._order[pos - 1]
        return self._order[-1] if (self.mode == "all" or self.shuffle) else None

    def jump(self, position):
        """Salta al item con position = posición en la lista visual."""
        try:
            self.index = position
            return self.items[position]
        except (IndexError, TypeError):
            return None

    def remove(self, pos):
        if 0 <= pos < len(self.items):
            removed_id = self.items[pos]["id"]
            cur = self.index
            self.items.pop(pos)
            if pos < cur:
                self.index -= 1
            if self.items:
                self.index = max(0, min(self.index, len(self.items) - 1))
            else:
                self.index = -1
            self._rebuild_order()
            return True
        return False

    def clear(self):
        self.items, self.index, self.context = [], -1, None
        self._order = []

    # ------------------------------------------------------------------ #
    def serialize(self):
        return {"items": [self.items[i] for i in self._order]
                if self._order else self.items,
                "index": self.index,
                "mode": self.mode, "shuffle": self.shuffle,
                "context": self.context,
                "pos": max(0, self.index)}

    def restore(self, data):
        items = data.get("items") or []
        self.items = items
        self.mode = data.get("mode", "none")
        self.shuffle = bool(data.get("shuffle"))
        ctx = data.get("context")
        # JSON no tiene tuplas: ("album", id) vuelta del disco es lista;
        # el app compara el contexto contra tuplas → devolvemos tuple
        if isinstance(ctx, list):
            try:
                ctx = tuple(ctx)
            except TypeError:
                ctx = None
        self.context = ctx
        pos = data.get("pos", -1)
        self.index = pos if 0 <= pos < len(items) else -1
        self._rebuild_order()
