"""#1473: the Discord batch color question.

One message carries one select menu per color group, five rows to a
message; the next groups spill to a second message. A message stays live
until every select on it is answered, and only then is it edited to show
the picks. Each select resolves its own registry prompt with the chosen
label, which is what the waiting intake collects.
"""
import asyncio
from types import SimpleNamespace as S

from print_handy.bot.adapter import StandaloneAdapter
from print_handy.core import prompts

CHAT = "123456789012345678"


def run(coro):
    return asyncio.run(coro)


class _Message:
    def __init__(self, channel, mid):
        self.channel = channel
        self.id = mid
        self.edits = []

    async def edit(self, content=None, view=None, **_kw):
        self.edits.append({"content": content, "view": view})
        return self


class _Channel:
    id = int(CHAT)

    def __init__(self):
        self.sent = []
        self._next = 1000

    async def send(self, content=None, view=None, **_kw):
        self._next += 1
        message = _Message(self, self._next)
        self.sent.append({"content": content, "view": view, "message": message})
        return message


class _Client:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, _chat_id):
        return self.channel


class _Response:
    def __init__(self, interaction):
        self.interaction = interaction

    async def defer(self):
        self.interaction.deferred = True

    async def send_message(self, content, ephemeral=False):
        self.interaction.replies.append(content)


class _Interaction:
    def __init__(self, custom_id, value):
        self.data = {"custom_id": custom_id, "values": [value]}
        self.user = S(id="1", display_name="Op")
        self.channel_id = int(CHAT)
        self.message = None
        self.deferred = False
        self.replies = []
        self.edited = []
        self.response = _Response(self)
        self.followup = S(send=self._followup)

    async def _followup(self, content, ephemeral=False):
        self.replies.append(content)

    async def edit_original_response(self, content=None, view=None, **_kw):
        self.edited.append({"content": content, "view": view})


def _groups(count):
    groups = []
    for i in range(count):
        cid = f"batchcid{i:02d}00000000000000000000"[:32]
        labels = ["Black PLA+", "Bronze PLA+"]
        prompts.register(cid, "batch-test", "Choose colors", labels)
        groups.append({"label": f"Part group {i + 1}", "choices": labels, "clarify_id": cid})
    return groups


def test_six_groups_render_as_two_messages_five_rows_then_one():
    async def flow():
        channel = _Channel()
        adapter = StandaloneAdapter(_Client(channel), ["1"])
        groups = _groups(6)
        result = await adapter.send_clarify_batch(CHAT, "Choose colors for these parts:",
                                                  groups, "batch-test")
        assert result.success
        views = [entry for entry in channel.sent if entry["view"] is not None]
        assert len(views) == 2
        assert len(views[0]["view"].children) == 5
        assert len(views[1]["view"].children) == 1
        # Answer the second message's only select: it completes at once.
        select = views[1]["view"].children[0]
        interaction = _Interaction(select.custom_id, "Bronze PLA+")
        assert await adapter.handle_click(interaction) is True
        assert prompts._prompts[groups[5]["clarify_id"]].answer == "Bronze PLA+"
        assert len(interaction.edited) == 1
        assert "Part group 6: **Bronze PLA+**" in interaction.edited[0]["content"]
        # The first message stays untouched until all five of its selects answer.
        first_interactions = []
        for child in views[0]["view"].children:
            interaction = _Interaction(child.custom_id, "Black PLA+")
            first_interactions.append(interaction)
            assert await adapter.handle_click(interaction) is True
        assert all(not i.edited for i in first_interactions[:-1])
        final = first_interactions[-1]
        assert len(final.edited) == 1
        for i in range(5):
            cid = groups[i]["clarify_id"]
            assert prompts._prompts[cid].answer == "Black PLA+"
            assert f"Part group {i + 1}: **Black PLA+**" in final.edited[0]["content"]
    run(flow())


def test_batch_prompt_is_not_stripped_while_sibling_selects_are_open():
    async def flow():
        channel = _Channel()
        adapter = StandaloneAdapter(_Client(channel), ["1"])
        groups = _groups(2)
        await adapter.send_clarify_batch(CHAT, "Choose:", groups, "batch-test")
        message = channel.sent[-1]["message"]
        await adapter.close_prompt(groups[0]["clarify_id"], "expired")
        assert message.edits == []  # the other select is still live on this message
        await adapter.close_prompt(groups[1]["clarify_id"], "expired")
        assert len(message.edits) == 1  # last close strips the message
    run(flow())
