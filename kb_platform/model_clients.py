class OwnedModelClients:
    def __init__(self, generation, embedding):
        self.values = generation, embedding
        self._unclaimed = list(dict((id(value), value) for value in self.values).values())
        self._claim_started = False

    def claim(self, context):
        if self._claim_started:
            raise ValueError('MODEL_CLIENT_OWNERSHIP_ALREADY_CLAIMED')
        self._claim_started = True
        while self._unclaimed:
            client = self._unclaimed[0]
            context.defer(client.close)
            self._unclaimed.pop(0)
        return self.values

    def close_unclaimed(self):
        while self._unclaimed:
            self._unclaimed[-1].close()
            self._unclaimed.pop()
