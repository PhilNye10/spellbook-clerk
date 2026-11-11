import sys, types
sys.modules.setdefault('discord', types.SimpleNamespace(Message=object))
from mtg_table_bot.fallback import parse_sesh_rsvps

class FakeEmbed:
    title = 'Draft Night'
    description = 'Attendees (2)\n<@11111111111111111>\n<@22222222222222222>\nMaybe (1)\n<@33333333333333333>\nNo (1)\n<@44444444444444444>'
    fields = []
    footer = None

class FakeMessage:
    content = ''
    embeds = [FakeEmbed()]

def test_sesh_rsvp_sections_are_separated():
    result = parse_sesh_rsvps(FakeMessage())
    assert result['going'] == ['11111111111111111', '22222222222222222']
    assert result['maybe'] == ['33333333333333333']
    assert result['no'] == ['44444444444444444']

class FakeField:
    def __init__(self, name, value):
        self.name = name
        self.value = value


def test_emoji_headings_keep_no_players_out_of_going():
    class FieldEmbed:
        title = 'Draft Night'
        description = ''
        fields = [
            FakeField('📅 Time', 'Wednesday'),
            FakeField('✅ Attendees (8)', '<@11111111111111111>\n<@22222222222222222>'),
            FakeField('🧑 Maybe (0)', '-'),
            FakeField('❌ No (2)', '<@33333333333333333>\n<@44444444444444444>'),
        ]
        footer = None
    class Msg:
        content = ''
        embeds = [FieldEmbed()]
    result = parse_sesh_rsvps(Msg())
    assert result['going'] == ['11111111111111111', '22222222222222222']
    assert result['maybe'] == []
    assert result['no'] == ['33333333333333333', '44444444444444444']
