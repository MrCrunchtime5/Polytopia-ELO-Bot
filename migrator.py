from playhouse.migrate import *
import settings
from playhouse.postgres_ext import *
import logging
from logging.handlers import RotatingFileHandler
from modules.models import Game

# http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#schema-migrations
handler = RotatingFileHandler(filename='discord.log', encoding='utf-8', maxBytes=500 * 1024, backupCount=1)
handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))

my_logger = logging.getLogger('polybot')
my_logger.setLevel(logging.DEBUG)
my_logger.addHandler(handler)  # root handler for app. module-specific loggers will inherit this

logger_peewee = logging.getLogger('peewee')
logger_peewee.setLevel(logging.DEBUG)

if (logger_peewee.hasHandlers()):
    logger_peewee.handlers.clear()

logger_peewee.addHandler(handler)

logger = logging.getLogger('polybot.' + __name__)

db = PostgresqlDatabase(settings.psql_db, user=settings.psql_user)
migrator = PostgresqlMigrator(db)

# is_ranked = BooleanField(default=True)
# elo_max = SmallIntegerField(default=1000)
# is_banned = BooleanField(default=False)
# required_role_id = BitField(default=None, null=True)
# timezone_offset = SmallIntegerField(default=None, null=True)
# win_claimed_ts = DateTimeField(null=True, default=None)
# win_confirmed = BooleanField(default=False)
# elo_after_game = SmallIntegerField(default=None, null=True)
# elo_change_team_alltime = SmallIntegerField(default=0)
# elo_alltime = SmallIntegerField(default=1000)
# game_chan = BitField(default=None, null=True)
# pro_league = BooleanField(default=True)
# date_polychamps_invite_sent = DateField(default=None, null=True)
# external_server = BitField(unique=False, null=True)
# team_chan_external_server = BitField(unique=False, null=True, default=None)
# tribe_direct = ForeignKeyField(Tribe, null=True, on_delete='SET NULL', field=Tribe.id)
# emoji = TextField(null=False, default='')
# elo_after_game_global = SmallIntegerField(default=None, null=True)
# team_elo_after_game = SmallIntegerField(default=None, null=True)
# team_elo_after_game_alltime = SmallIntegerField(default=None, null=True)
# size = ArrayField(SmallIntegerField, default=0)
guild_id = BitField(unique=False, null=False, default=0)

migrate(
    # migrator.add_column('discordmember', 'elo_max', elo_max),
    # migrator.add_column('player', 'is_banned', is_banned),
    # migrator.add_column('discordmember', 'is_banned', is_banned),
    # migrator.add_column('gameside', 'required_role_id', required_role_id)
    # migrator.add_column('discordmember', 'timezone_offset', timezone_offset),
    # migrator.add_column('game', 'win_claimed_ts', win_claimed_ts),
    # migrator.add_column('gameside', 'win_confirmed', win_confirmed)
    # migrator.add_column('gameside', 'elo_change_team_alltime', elo_change_team_alltime),
    # migrator.add_column('team', 'elo_alltime', elo_alltime)
    # migrator.add_column('discordmember', 'date_polychamps_invite_sent', date_polychamps_invite_sent)
    # migrator.add_column('gameside', 'team_chan_external_server', external_server),
    # migrator.add_column('team', 'external_server', external_server)
    # migrator.add_column('lineup', 'tribe_direct_id', tribe_direct)
    # migrator.drop_column('tribe', 'emoji'),
    # migrator.add_column('lineup', 'elo_after_game_global', elo_after_game_global),
    # migrator.add_column('gameside', 'team_elo_after_game', team_elo_after_game),
    # migrator.add_column('gameside', 'team_elo_after_game_alltime', team_elo_after_game_alltime)
    migrator.add_column('gamelog', 'guild_id', guild_id),
    # migrator.drop_column('game', 'size'),
    # migrator.alter_column_type('gamelog', 'game_id', ForeignKeyField(Game))
    # migrator.drop_constraint('gamelog', 'gamelog_game_id_fkey')

)

# import modules.models as models
# models.db.connect()
# for g in models.Game.select():
#     size = [s.size for s in g.gamesides]
#     # print(g.id, size)
#     g.size = size
#     g.save()

print('done')
