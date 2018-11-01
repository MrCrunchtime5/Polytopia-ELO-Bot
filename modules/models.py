import datetime
import discord
from peewee import *
from playhouse.postgres_ext import *
import modules.exceptions as exceptions
# from modules import utilities
from modules import channels
import settings
import logging

logger = logging.getLogger('polybot.' + __name__)

# db = PostgresqlDatabase(settings.psql_db, user=settings.psql_user)
db = PostgresqlDatabase('polytopia_dev2', user=settings.psql_user)


def tomorrow():
    return (datetime.datetime.now() + datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")


class BaseModel(Model):
    class Meta:
        database = db


class Team(BaseModel):
    name = TextField(unique=False, null=False)       # can't store in case insensitive way, need to use ILIKE operator
    elo = SmallIntegerField(default=1000)
    emoji = TextField(null=False, default='')
    image_url = TextField(null=True)
    guild_id = BitField(unique=False, null=False)    # Included for possible future expanson
    is_hidden = BooleanField(default=False)             # True / generic team ie Home/Away, False = server team like Ronin

    class Meta:
        indexes = ((('name', 'guild_id'), True),)   # Trailing comma is required
        # http://docs.peewee-orm.com/en/3.6.0/peewee/models.html#multi-column-indexes

    def get_by_name(team_name: str, guild_id: int):
        teams = Team.select().where((Team.name.contains(team_name)) & (Team.guild_id == guild_id))
        return teams

    def get_or_except(team_name: str, guild_id: int):
        results = Team.get_by_name(team_name=team_name, guild_id=guild_id)
        if len(results) == 0:
            raise exceptions.NoMatches(f'No matching team was found for "{team_name}"')
        if len(results) > 1:
            raise exceptions.TooManyMatches(f'More than one matching team was found for "{team_name}"')

        return results[0]

    def completed_game_count(self):

        num_games = GameSide.select().join(Game).where(
            (GameSide.team == self) & (GameSide.game.is_completed == 1)
        ).count()

        return num_games

    def change_elo_after_game(self, chance_of_winning: float, is_winner: bool):
        print(f'Team CoW: {chance_of_winning}')
        if self.completed_game_count() < 11:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        if is_winner is True:
            elo_delta = int(round((max_elo_delta * (1 - chance_of_winning)), 0))
        else:
            elo_delta = int(round((max_elo_delta * (0 - chance_of_winning)), 0))

        self.elo = int(self.elo + elo_delta)
        self.save()

        return elo_delta

    def team_games_subq():

        q = Lineup.select(Lineup.game).join(Game).group_by(Lineup.game).having(fn.COUNT('*') > 2)
        return q

    def get_record(self):

        wins = GameSide.select().join(Game).where(
            (Game.id.in_(Team.team_games_subq())) & (Game.is_completed == 1) & (GameSide.team == self) & (GameSide.id == Game.winner)
        ).count()

        losses = GameSide.select().join(Game).where(
            (Game.id.in_(Team.team_games_subq())) & (Game.is_completed == 1) & (GameSide.team == self) & (GameSide.id != Game.winner)
        ).count()

        return (wins, losses)


class DiscordMember(BaseModel):
    discord_id = BitField(unique=True, null=False)
    name = TextField(unique=False)
    elo = SmallIntegerField(default=1000)
    polytopia_id = TextField(null=True)
    polytopia_name = TextField(null=True)


class Player(BaseModel):
    discord_member = ForeignKeyField(DiscordMember, unique=False, null=False, backref='guildmember', on_delete='CASCADE')
    guild_id = BitField(unique=False, null=False)
    nick = TextField(unique=False, null=True)
    name = TextField(unique=False, null=True)
    team = ForeignKeyField(Team, null=True, backref='player', on_delete='SET NULL')
    elo = SmallIntegerField(default=1000)
    trophies = ArrayField(CharField, null=True)
    # Add discord name here too so searches can hit just one table?

    def generate_display_name(self=None, player_name=None, player_nick=None):
        if player_nick:
            if player_name in player_nick:
                display_name = player_nick
            else:
                display_name = f'{player_name} ({player_nick})'
        else:
            display_name = player_name

        if self:
            self.name = display_name
            self.save()
        return display_name

    def upsert(discord_id, guild_id, discord_name=None, discord_nick=None, team=None):
        # Stopped using postgres upsert on_conflict() because it only returns row ID so its annoying to use
        display_name = Player.generate_display_name(player_name=discord_name, player_nick=discord_nick)

        try:
            with db.atomic():
                discord_member = DiscordMember.create(discord_id=discord_id, name=discord_name)
        except IntegrityError:
            discord_member = DiscordMember.get(discord_id=discord_id)
            discord_member.name = discord_name
            discord_member.save()

        try:
            with db.atomic():
                player = Player.create(discord_member=discord_member, guild_id=guild_id, nick=discord_nick, name=display_name, team=team)
            created = True
        except IntegrityError:
            created = False
            player = Player.get(discord_member=discord_member, guild_id=guild_id)
            if display_name:
                player.name = display_name
            if team:
                player.team = team
            if discord_nick:
                player.nick = discord_nick
            player.save()

        return player, created

    def get_teams_of_players(guild_id, list_of_players):
        # TODO: make function async? Tried but got invalid syntax complaint in linter in the calling function

        # given [List, Of, discord.Member, Objects] - return a, b
        # a = binary flag if all members are on the same Poly team. b = [list] of the Team objects from table the players are on
        # input: [Nelluk, Frodakcin]
        # output: True, [<Ronin>, <Ronin>]

        def get_matching_roles(discord_member, list_of_role_names):
            # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.
            member_roles = [x.name for x in discord_member.roles]
            return set(member_roles).intersection(list_of_role_names)

        query = Team.select(Team.name).where(Team.guild_id == guild_id)
        list_of_teams = [team.name for team in query]               # ['The Ronin', 'The Jets', ...]
        list_of_matching_teams = []
        for player in list_of_players:
            matching_roles = get_matching_roles(player, list_of_teams)
            if len(matching_roles) == 1:
                # TODO: This would be more efficient to do as one query and then looping over the list of teams one time for each player
                name = next(iter(matching_roles))
                list_of_matching_teams.append(
                    Team.select().where(
                        (Team.name == name) & (Team.guild_id == guild_id)
                    ).get()
                )
            else:
                list_of_matching_teams.append(None)
                # Would be here if no player Roles match any known teams, -or- if they have more than one match

        same_team_flag = True if all(x == list_of_matching_teams[0] for x in list_of_matching_teams) else False
        return same_team_flag, list_of_matching_teams

    def string_matches(player_string: str, guild_id: int):
        # Returns QuerySet containing players in current guild matching string. Searches against discord mention ID first, then exact discord name match,
        # then falls back to substring match on name/nick, then a lastly a substring match of polytopia ID or polytopia in-game name

        try:
            p_id = int(player_string.strip('<>!@'))
        except ValueError:
            pass
        else:
            # lookup either on <@####> mention string or raw ID #
            return Player.select(Player, DiscordMember).join(DiscordMember).where(
                (DiscordMember.discord_id == p_id) & (Player.guild_id == guild_id)
            )

        if len(player_string.split('#', 1)[0]) > 2:
            discord_str = player_string.split('#', 1)[0]
            # If query is something like 'Nelluk#7034', use just the 'Nelluk' to match against discord_name.
            # This happens if user does an @Mention then removes the @ character
        else:
            discord_str = player_string

        name_exact_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
            (DiscordMember.name == discord_str) & (Player.guild_id == guild_id)
        )
        if name_exact_match.count() == 1:
            # String matches DiscordUser.name exactly
            return name_exact_match

        # If no exact match, return any substring matches
        name_substring_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
            ((Player.nick.contains(player_string)) | (DiscordMember.name.contains(discord_str))) & (Player.guild_id == guild_id)
        )

        if name_substring_match.count() > 0:
            return name_substring_match

        # If no substring name matches, return anything with matching polytopia name or code
        poly_fields_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
            ((DiscordMember.polytopia_id.contains(player_string)) | (DiscordMember.polytopia_name.contains(player_string))) & (Player.guild_id == guild_id)
        )
        return poly_fields_match

    def get_or_except(player_string: str, guild_id: int):
        results = Player.string_matches(player_string=player_string, guild_id=guild_id)
        if len(results) == 0:
            raise exceptions.NoMatches(f'No matching player was found for "{player_string}"')
        if len(results) > 1:
            raise exceptions.TooManyMatches(f'More than one matching player was found for "{player_string}"')

        return results[0]

    def get_by_discord_id(discord_id: int, guild_id: int, discord_nick: str = None, discord_name: str = None):
        # if no matching player, will check to see if there is already a DiscordMember created from another guild's player
        # if exists, Player will be upserted
        # return PlayerObj, Bool. bool = True if player was upserted

        try:
            player = Player.select().join(DiscordMember).where(
                (DiscordMember.discord_id == discord_id) & (Player.guild_id == guild_id)).get()
            return player, False
        except DoesNotExist:
            pass

        # no current player. check to see if DiscordMember exists
        try:
            _ = DiscordMember.get(discord_id=discord_id)
        except DoesNotExist:
            # No matching player or discordmember
            return None, False
        else:
            # DiscordMember found, upserting new player
            player, _ = Player.upsert(discord_id=discord_id, discord_name=discord_name, discord_nick=discord_nick, guild_id=guild_id)
            logger.info(f'Upserting new player for discord ID {discord_id}')
            return player, True

    def completed_game_count(self):

        num_games = Lineup.select().join(Game).where(
            (Lineup.game.is_completed == 1) & (Lineup.player == self)
        ).count()

        return num_games

    def wins(self):
        # TODO: Could combine wins/losses into one function that takes an argument and modifies query

        q = Lineup.select().join(Game).join_from(Lineup, GameSide).where(
            (Lineup.game.is_completed == 1) & (Lineup.player == self) & (Game.winner == Lineup.gameside.id)
        )

        return q

    def losses(self):
        q = Lineup.select().join(Game).join_from(Lineup, GameSide).where(
            (Lineup.game.is_completed == 1) & (Lineup.player == self) & (Game.winner != Lineup.gameside.id)
        )

        return q

    def get_record(self):

        return (self.wins().count(), self.losses().count())

    def leaderboard_rank(self, date_cutoff):
        # TODO: This could be replaced with Postgresql Window functions to have the DB calculate the rank.
        # Advantages: Probably moderately more efficient, and will resolve ties in a sensible way
        # But no idea how to write the query :/

        query = Player.leaderboard(date_cutoff=date_cutoff, guild_id=self.guild_id)

        player_found = False
        for counter, p in enumerate(query.tuples()):
            if p[0] == self.id:
                player_found = True
                break

        rank = counter + 1 if player_found else None
        return (rank, query.count())

    def leaderboard(date_cutoff, guild_id: int):
        query = Player.select().join(Lineup).join(Game).where(
            (Player.guild_id == guild_id) & (Game.is_completed == 1) & (Game.date > date_cutoff)
        ).distinct().order_by(-Player.elo)

        if query.count() < 10:
            # Include all registered players on leaderboard if not many games played
            query = Player.select().where(Player.guild_id == guild_id).order_by(-Player.elo)

        return query

    def favorite_tribes(self, limit=3):
        # Returns a list of dicts of format:
        # {'tribe': 7, 'emoji': '<:luxidoor:448015285212151809>', 'name': 'Luxidoor', 'tribe_count': 14}

        # TODO: take into account tribe selections from parent DiscordMember, not just single guild-player. Should be a relatively easy change

        q = Lineup.select(Lineup.tribe, TribeFlair.emoji, Tribe.name, fn.COUNT(Lineup.tribe).alias('tribe_count')).join(TribeFlair).join(Tribe).where(
            (Lineup.player == self) & (Lineup.tribe.is_null(False))
        ).group_by(Lineup.tribe, Lineup.tribe.emoji, Tribe.name).order_by(-SQL('tribe_count')).limit(limit)

        return q.dicts()

    class Meta:
        indexes = ((('discord_member', 'guild_id'), True),)   # Trailing comma is required


class Tribe(BaseModel):
    name = TextField(unique=True, null=False)


class TribeFlair(BaseModel):
    tribe = ForeignKeyField(Tribe, unique=False, null=False, on_delete='CASCADE')
    emoji = TextField(null=False, default='')
    guild_id = BitField(unique=False, null=False)

    class Meta:
        indexes = ((('tribe', 'guild_id'), True),)   # Trailing comma is required
        # http://docs.peewee-orm.com/en/3.6.0/peewee/models.html#multi-column-indexes

    def get_by_name(name: str, guild_id: int):
        tribe_flair_match = TribeFlair.select(TribeFlair, Tribe).join(Tribe).where(
            (Tribe.name.contains(name)) & (TribeFlair.guild_id == guild_id)
        )

        tribe_name_match = Tribe.select().where(Tribe.name.contains(name))

        if tribe_flair_match.count() == 0:
            if tribe_name_match.count() == 0:
                logger.warn(f'No TribeFlair -or- Tribe could be matched to {name}')
                return None
            else:
                logger.warn(f'No TribeFlair for this guild matched to {tribe_name_match[0].name}. Creating TribeFlair with blank emoji.')
                with db.atomic():
                    new_tribeflair = TribeFlair.create(tribe=tribe_name_match[0], guild_id=guild_id)
                    return new_tribeflair
        else:
            return tribe_flair_match[0]

    def upsert(name: str, guild_id: int, emoji: str):
        try:
            tribe = Tribe.get(Tribe.name.contains(name))
        except DoesNotExist:
            raise exceptions.CheckFailedError(f'Could not find any tribe name containing "{name}"')

        tribeflair, created = TribeFlair.get_or_create(tribe=tribe, guild_id=guild_id, defaults={'emoji': emoji})
        if not created:
            tribeflair.emoji = emoji
            tribeflair.save()

        return tribeflair


class Game(BaseModel):
    name = TextField(null=True)
    is_completed = BooleanField(default=False)
    is_confirmed = BooleanField(default=False)  # Use to confirm losses and filter searches?
    announcement_message = BitField(default=None, null=True)
    announcement_channel = BitField(default=None, null=True)
    date = DateField(default=datetime.datetime.today)
    completed_ts = DateTimeField(null=True, default=None)
    name = TextField(null=True)
    winner = DeferredForeignKey('GameSide', null=True, on_delete='RESTRICT')
    guild_id = BitField(unique=False, null=False)
    host = ForeignKeyField(Player, null=True, backref='hosting', on_delete='SET NULL')
    expiration = DateTimeField(null=True, default=tomorrow)  # For pending/matchmaking status
    notes = TextField(null=True)
    is_pending = BooleanField(default=False)

    async def create_squad_channels(self, ctx):
        game_roster = []
        for gameside in self.gamesides:
            game_roster.append([r[0].name for r in gameside.roster()])

        roster_names = ' -vs- '.join([' '.join(side) for side in game_roster])
        # yields a string like 'Player1 Player2 -vs- Player3 Player4'

        for gameside in self.gameside:
            player_list = [r[0] for r in gameside.roster()]
            if len(player_list) < 2:
                continue
            chan = await channels.create_squad_channel(ctx, game=self, team_name=gameside.team.name, player_list=player_list)
            if chan:
                gameside.team_chan = chan.id
                gameside.save()

                await channels.greet_squad_channel(ctx, chan=chan, player_list=player_list, roster_names=roster_names, game=self)

    async def delete_squad_channels(self, ctx):

        if self.name.lower()[:2] == 's3' or self.name.lower()[:2] == 's4' or self.name.lower()[:2] == 's5':
            return logger.warn(f'Skipping team channel deletion for game {self.id} {self.name} since it is a Season game')

        for gameside in self.gamesides:
            if gameside.team_chan:
                await channels.delete_squad_channel(ctx, channel_id=gameside.team_chan)
                gameside.team_chan = None
                gameside.save()

    async def update_squad_channels(self, ctx):

        for gameside in self.gamesides:
            if gameside.team_chan:
                await channels.update_squad_channel_name(ctx, channel_id=gameside.team_chan, game_id=self.id, game_name=self.name, team_name=gameside.team.name)

    async def update_announcement(self, ctx):
        # Updates contents of new game announcement with updated game_embed card

        if self.announcement_channel is None or self.announcement_message is None:
            return
        channel = ctx.guild.get_channel(self.announcement_channel)
        if channel is None:
            return logger.warn('Couldn\'t get channel in update_announacement')

        try:
            message = await channel.get_message(self.announcement_message)
        except (discord.errors.Forbidden, discord.errors.NotFound, discord.errors.HTTPException):
            return logger.warn('Couldn\'t get message in update_announacement')

        try:
            embed, content = self.embed(ctx)
            await message.edit(embed=embed, content=content)
        except discord.errors.HTTPException:
            return logger.warn('Couldn\'t update message in update_announacement')

    def is_hosted_by(self, discord_id: int):

        return self.host.discord_member.discord_id == discord_id, self.host

    def embed(self, ctx):

        embed = discord.Embed(title=f'{self.get_headline()} — *{self.size_string()}*'[:255])

        if self.is_completed == 1:
            embed.title += f'\n\nWINNER: {self.winner.name()}'

            # Set embed image (profile picture or team logo)
            if len(self.winner.lineup) == 1:
                # Winner is individual player
                winning_discord_member = ctx.guild.get_member(self.winner.lineup[0].player.discord_member.discord_id)
                if winning_discord_member is not None:
                    embed.set_thumbnail(url=winning_discord_member.avatar_url_as(size=512))
            elif self.winner.team and self.winner.team.image_url:
                # Winner is a team of players - use team image if present
                embed.set_thumbnail(url=self.winner.team.image_url)

        game_data = []
        for gameside in self.gamesides:
            team_elo_str, squad_elo_str = gameside.elo_strings()

            if not gameside.team or gameside.team.is_hidden:
                # Hide team ELO if generic Team
                team_elo_str = '\u200b'

            if len(gameside.lineup) == 1:
                # Hide gamesides ELO stats for 1-player teams
                squad_elo_str = '\u200b'

            game_data.append((gameside, team_elo_str, squad_elo_str, gameside.roster()))

        use_separator = False
        for side, elo_str, squad_str, roster in game_data:

            if use_separator:
                embed.add_field(name='\u200b', value='\u200b', inline=False)  # Separator between sides

            if len(side.lineup) > 1:
                team_str = f'__Lineup for Team **{side.team.name if side.team else "None"}**__ {elo_str}'

                embed.add_field(name=team_str, value=squad_str, inline=False)

            for player, player_elo_str, tribe_emoji in roster:
                if len(side.lineup) > 1:
                    embed.add_field(name=f'**{player.name}** {tribe_emoji}', value=f'ELO: {player_elo_str}', inline=True)
                else:
                    embed.add_field(name=f'__**{player.name}**__ {tribe_emoji}', value=f'ELO: {player_elo_str}', inline=True)
            use_separator = True

        if self.match:
            notes = f'\n**Notes:** {self.match[0].notes}' if self.match[0].notes else ''
            embed_content = f'Matchmaking **M{self.match[0].id}**{notes}'
        else:
            embed_content = None

        if ctx.guild.id != settings.server_ids['polychampions']:
            embed.add_field(value='Powered by **PolyChampions** - https://discord.gg/cX7Ptnv', name='\u200b', inline=False)
            embed.set_author(name='PolyChampions', url='https://discord.gg/cX7Ptnv', icon_url='https://cdn.discordapp.com/emojis/488510815893323787.png?v=1')

        if not self.is_completed:
            status_str = 'Incomplete'
        elif self.is_confirmed:
            status_str = 'Completed'
        else:
            status_str = 'Unconfirmed'

        embed.set_footer(text=f'Status: {status_str}  -  Creation Date {str(self.date)}')

        return embed, embed_content

    def get_headline(self):
        # yields string like:
        # Game 481   :fried_shrimp: The Crawfish vs :fried_shrimp: TestAccount1 vs :spy: TestBoye1\n*Name of Game*
        gameside_strings = []
        for gameside in self.gamesides:
            emoji = ''
            if gameside.team:
                if len(gameside.lineup) > 1 or not gameside.team.is_hidden:
                    emoji = gameside.team.emoji

            gameside_strings.append(f'{emoji} **{gameside.name()}**')
        full_squad_string = ' *vs* '.join(gameside_strings)[:225]

        game_name = f'\n\u00a0*{self.name}*' if self.name and self.name.strip() else ''
        # \u00a0 is used as an invisible delimeter so game_name can be split out easily
        return f'Game {self.id}   {full_squad_string}{game_name}'

    def largest_team(self):
        return max(len(gameside.lineup) for gameside in self.gamesides)

    def size_string(self):

        if self.is_pending:
            # use capacity for matchmaking strings
            if max(s.size for s in self.gamesides) and len(self.gamesides) > 2:
                return 'FFA'
            else:
                return 'v'.join(str(s.size) for s in self.gamesides)

        # this might be superfluous, combined Match and Game functions together
        if self.largest_team() == 1 and len(self.gamesides) > 2:
            return 'FFA'
        else:
            return 'v'.join(str(len(s.lineup)) for s in self.gamesides)

    def load_full_game(game_id: int):
        # Returns a single Game object with all related tables pre-fetched. or None

        game = Game.select().where(Game.id == game_id)
        subq = GameSide.select(GameSide, Team).join(Team, JOIN.LEFT_OUTER).join_from(GameSide, Squad, JOIN.LEFT_OUTER)

        subq2 = Lineup.select(
            Lineup, Tribe, TribeFlair, Player, DiscordMember).join(
            TribeFlair, JOIN.LEFT_OUTER).join(  # Need LEFT_OUTER_JOIN - default inner join would only return records that have a Tribe chosen
            Tribe, JOIN.LEFT_OUTER).join_from(
            Lineup, Player).join_from(Player, DiscordMember)

        res = prefetch(game, subq, subq2)

        if len(res) == 0:
            raise DoesNotExist()
        return res[0]

    def create_game(discord_groups, guild_id, name: str = None, require_teams: bool = False):
        # discord_groups = list of lists [[d1, d2, d3], [d4, d5, d6]]. each item being a discord.Member object

        generic_teams_short = [('Home', ':stadium:'), ('Away', ':airplane:')]  # For two-team games
        generic_teams_long = [('Sharks', ':shark:'), ('Owls', ':owl:'), ('Eagles', ':eagle:'), ('Tigers', ':tiger:'),
                              ('Bears', ':bear:'), ('Koalas', ':koala:'), ('Dogs', ':dog:'), ('Bats', ':bat:'),
                              ('Lions', ':lion:'), ('Cats', ':cat:'), ('Birds', ':bird:'), ('Spiders', ':spider:')]

        list_of_detected_teams, list_of_final_teams, teams_for_each_discord_member = [], [], []
        intermingled_flag = False
        # False if all players on each side belong to the same server team, Ronin/Jets.True if players are mixed or on a server without teams

        for discord_group in discord_groups:
            same_team, list_of_teams = Player.get_teams_of_players(guild_id=guild_id, list_of_players=discord_group)
            teams_for_each_discord_member.append(list_of_teams)  # [[Team, Team][Team, Team]] for each team that a discord member is associated with, for Player.upsert()
            if None in list_of_teams:
                if require_teams is True:
                    raise exceptions.CheckFailedError('One or more players listed cannot be matched to a Team (based on Discord Roles). Make sure player has exactly one matching Team role.')
                else:
                    # Player(s) can't be matched to team, but server setting allows that.
                    intermingled_flag = True
            if not same_team:
                # Mixed players within same side
                intermingled_flag = True

            if not intermingled_flag:
                if list_of_teams[0] in list_of_detected_teams:
                    # Detected team already present (ie. Ronin players vs Ronin players)
                    intermingled_flag = True
                else:
                    list_of_detected_teams.append(list_of_teams[0])

        if not intermingled_flag:
            # Use detected server teams for this game
            assert len(list_of_detected_teams) == len(discord_groups), 'Mismatched lists!'
            list_of_final_teams = list_of_detected_teams
        else:
            # Use Generic Teams
            if len(discord_groups) == 2:
                generic_teams = generic_teams_short
            else:
                generic_teams = generic_teams_long

            for count in range(len(discord_groups)):
                team_obj, created = Team.get_or_create(name=generic_teams[count][0], guild_id=guild_id,
                                                       defaults={'emoji': generic_teams[count][1], 'is_hidden': True})
                list_of_final_teams.append(team_obj)

        with db.atomic():
            newgame = Game.create(name=name.strip('\"').strip('\'').title()[:35],
                                  guild_id=guild_id)

            # print(discord_groups)
            for team_group, allied_team, discord_group in zip(teams_for_each_discord_member, list_of_final_teams, discord_groups):
                # team_group is each team that the individual discord.Member is associated with on the server, often None
                # allied_team is the team that this entire group is playing for in this game. Either a Server Team or Generic. Never None.

                player_group = []
                # print('dg', len(team_group), len(discord_group), discord_group)
                for team, discord_member in zip(team_group, discord_group):
                    # print(team, discord_member)
                    # Upsert each discord.Member into a Player database object
                    player_group.append(
                        Player.upsert(discord_id=discord_member.id, discord_name=discord_member.name, discord_nick=discord_member.nick, guild_id=guild_id, team=team)[0]
                    )

                # Create Squad records if 2+ players are allied
                if len(player_group) > 1:
                    squad = Squad.upsert(player_list=player_group, guild_id=guild_id)
                else:
                    squad = None

                gameside = GameSide.create(game=newgame, squad=squad, team=allied_team)

                # Create Lineup records
                for player in player_group:
                    Lineup.create(game=newgame, gameside=gameside, player=player)

        return newgame

    def reverse_elo_changes(self):
        for lineup in self.lineup:
            print(f'game {self.id} pre-revision - player: {lineup.player.elo}')
            lineup.player.elo += lineup.elo_change_player * -1
            lineup.player.save()
            lineup.elo_change_player = 0
            print(f'post-revision - player: {lineup.player.elo}')
            if lineup.elo_change_discordmember:
                lineup.player.discord_member.elo += lineup.elo_change_discordmember * -1
                lineup.elo_change_discordmember = 0
            lineup.save()

        for gameside in self.gameside:
            if gameside.elo_change_squad and gameside.squad:
                print(f'pre-revision - squad: {gameside.squad.elo}')
                gameside.squad.elo += (gameside.elo_change_squad * -1)
                gameside.squad.save()
                gameside.elo_change_squad = 0
                print(f'post-revision - squad: {gameside.squad.elo}')

            if gameside.elo_change_team and gameside.team:
                print(f'pre-revision - team: {gameside.team.elo}')
                gameside.team.elo += (gameside.elo_change_team * -1)
                gameside.team.save()
                gameside.elo_change_team = 0
                print(f'post-revision - team: {gameside.team.elo}')

            gameside.save()

    def delete_game(self):
        # resets any relevant ELO changes to players and teams, deletes related lineup records, and deletes the game entry itself

        with db.atomic():
            if self.winner:
                self.winner = None
                recalculate = True
                since = self.completed_ts

                self.reverse_elo_changes()
                self.save()
            else:
                recalculate = False

            for lineup in self.lineup:
                lineup.delete_instance()

            for gameside in self.gamesides:
                gameside.delete_instance()

            self.delete_instance()

            if recalculate:
                Game.recalculate_elo_since(timestamp=since)

    def get_side_win_chances(largest_team: int, gameside_list, gameside_elo_list):
        n = len(gameside_list)
        print(gameside_elo_list)

        # Adjust team elos when the amount of players on each team
        # is imbalanced, e.g. 1v2. It changes nothing when sizes are equal
        adjusted_side_elo, win_chance_list = [], []
        sum_elo = 0
        sum_raw_elo = sum(gameside_elo_list)
        for s, elo in zip(gameside_list, gameside_elo_list):
            missing_players = largest_team - len(s.lineup)
            avg_opponent_elos = int(round((sum_raw_elo - elo) / (n - 1)))
            adj_side_elo = s.adjusted_elo(missing_players, elo, avg_opponent_elos)
            adjusted_side_elo.append(adj_side_elo)
            sum_elo += adj_side_elo

        # Compute proper win chances when there are more than 2 teams,
        # e.g. 2v2v2. It changes nothing when there are only 2 teams
        win_chance_unnorm = []
        normalization_factor = 0
        for own_elo, side in zip(adjusted_side_elo, gameside_list):
            win_chance = GameSide.calc_win_chance(own_elo, (sum_elo - own_elo) / (n - 1))
            win_chance_unnorm.append(win_chance)
            normalization_factor += win_chance

        # Apply the win/loss results for each team given their win% chance
        # for i in range(n):
        for side_win_chance_unnorm, adj_side_elo, side in zip(win_chance_unnorm, adjusted_side_elo, gameside_list):
            win_chance = round(side_win_chance_unnorm / normalization_factor, 3)
            win_chance_list.append(win_chance)

        print(win_chance_list)
        return win_chance_list

    def declare_winner(self, winning_side: 'GameSide', confirm: bool):

        if winning_side.game != self:
            raise exceptions.CheckFailedError(f'GameSide id {winning_side.id} did not play in this game')

        if confirm is True:
            self.is_confirmed = True
            largest_side = self.largest_team()
            smallest_side = min(len(gameside.lineup) for gameside in self.gamesides)

            side_elos = [s.average_elo() for s in self.gamesides]
            team_elos = [s.team.elo if s.team else None for s in self.gamesides]
            squad_elos = [s.squad.elo if s.squad else None for s in self.gamesides]

            side_win_chances = Game.get_side_win_chances(largest_side, self.gamesides, side_elos)

            if smallest_side > 1:
                if None not in team_elos:
                    team_win_chances = Game.get_side_win_chances(largest_side, self.gamesides, team_elos)
                else:
                    team_win_chances = None

                if None not in squad_elos:
                    squad_win_chances = Game.get_side_win_chances(largest_side, self.gamesides, squad_elos)
                else:
                    squad_win_chances = None
            else:
                team_win_chances, squad_win_chances = None, None

            for i in range(len(self.gamesides)):
                side = self.gamesides[i]
                is_winner = True if side == winning_side else False
                for p in side.lineup:
                    p.change_elo_after_game(side_win_chances[i], is_winner)

                if team_win_chances:
                    side.elo_change_team = side.team.change_elo_after_game(team_win_chances[i], is_winner)
                if squad_win_chances:
                    side.elo_change_squad = side.squad.change_elo_after_game(squad_win_chances[i], is_winner)

                side.save()

        self.winner = winning_side
        self.is_completed = True
        self.completed_ts = datetime.datetime.now()
        self.save()

    def has_player(self, player: Player = None, discord_id: int = None):
        # if player (or discord_id) was a participant in this game: return True, GameSide
        # else, return False, None
        if player:
            discord_id = player.discord_member.discord_id

        if not discord_id:
            return (False, None)

        for l in self.lineup:
            if l.player.discord_member.discord_id == int(discord_id):
                return (True, l.gameside)
        return (False, None)

    def player(self, player: Player = None, discord_id: int = None):
        # return game.lineup, based on either Player object or discord_id. else None

        if player:
            discord_id = player.discord_member.discord_id

        if not discord_id:
            return None

        for l in self.lineup:
            if l.player.discord_member.discord_id == int(discord_id):
                return l
        return None

    def capacity(self):
        return (len(self.lineup), sum(s.size for s in self.gamesides))

    def gameside_by_name(self, ctx, name: str):
        # Given a string representing a game side's name (team name for 2+ players, player name for 1 player)
        # Return a tuple of the participant and their gameside, ie Player, GameSide or Team, gameside

        if len(name) < 3:
            raise exceptions.CheckFailedError('Name given is not enough characters. Be more specific')

        matches = []
        for gameside in self.gamesides:
            if len(gamesides.lineup) == 1:
                try:
                    p_id = int(name.strip('<>!@'))
                except ValueError:
                    # Compare to single gamesides player's name
                    if name.lower() in gameside.lineup[0].player.name.lower():
                        matches.append(
                            (gameside.lineup[0].player, gameside)
                        )
                else:
                    # name is a <@PlayerMention>
                    # compare to single squad player's discord ID
                    if p_id == gameside.lineup[0].player.discord_member.discord_id:
                        print('found by d.id')
                        return (gameside.lineup[0].player, gameside)
            else:
                # Compare to gamesidess team's name
                assert bool(gameside.team), 'GameSide obj has no team'
                if name.lower() in gameside.team.name.lower():
                    matches.append(
                        (gameside.team, gameside)
                    )

        if len(matches) == 1:
            return matches[0]
        if len(matches) == 0:
            raise exceptions.NoMatches(f'No matches found for "{name}" in game {self.id}.')
        else:
            raise exceptions.TooManyMatches(f'{len(matches)} matches found for "{name}" in game {self.id}.')

    def search(player_filter=None, team_filter=None, title_filter=None, status_filter: int = 0, guild_id: int = None):
        # Returns Games by almost any combination of player/team participation, and game status
        # player_filter/team_filter should be a [List, of, Player/Team, objects] (or ID #s)
        # status_filter:
        # 0 = all games, 1 = completed games, 2 = incomplete games
        # 3 = wins, 4 = losses (only for first player in player_list or, if empty, first team in team list)
        # 5 = unconfirmed wins

        confirmed_filter, completed_filter = [0, 1], [0, 1]

        if status_filter == 1:
            # completed games
            completed_filter = [1]
        elif status_filter == 2:
            # incomplete games
            completed_filter = [0]
        elif status_filter == 5:
            # Unconfirmed completed games
            completed_filter, confirmed_filter = [1], [0]

        if guild_id:
            guild_filter = Game.select(Game.id).where(Game.guild_id == guild_id)
        else:
            guild_filter = Game.select(Game.id)

        if team_filter:
            team_subq = GameSide.select(GameSide.game).join(Game).where(
                (GameSide.team.in_(team_filter)) & (GameSide.game.in_(Team.team_games_subq()))
            ).group_by(GameSide.game).having(
                fn.COUNT(GameSide.team) == len(team_filter)
            )
        else:
            team_subq = Game.select(Game.id)

        if player_filter:
            player_subq = Lineup.select(Lineup.game).join(Game).where(
                (Lineup.player.in_(player_filter))
            ).group_by(Lineup.game).having(
                fn.COUNT(Lineup.player) == len(player_filter)
            )
        else:
            player_subq = Game.select(Game.id)

        if title_filter:
            title_subq = Game.select(Game.id).where(Game.name.contains('%'.join(title_filter)))
        else:
            title_subq = Game.select(Game.id)

        if (not player_filter and not team_filter) or status_filter not in [3, 4]:
            # No filtering on wins/losses
            victory_subq = Game.select(Game.id)
        else:
            if player_filter:
                # Filter wins/losses on first entry in player_filter
                if status_filter == 3:
                    # Games that player has won
                    victory_subq = Lineup.select(Lineup.game).join(Game).join_from(Lineup, GameSide).where(
                        (Lineup.game.is_completed == 1) & (Lineup.player == player_filter[0]) & (Game.winner == Lineup.gameside.id)
                    )
                elif status_filter == 4:
                    # Games that player has lost
                    victory_subq = Lineup.select(Lineup.game).join(Game).join_from(Lineup, GameSide).where(
                        (Lineup.game.is_completed == 1) & (Lineup.player == player_filter[0]) & (Game.winner != Lineup.gameside.id)
                    )
            else:
                # Filter wins/losses on first entry in team_filter
                if status_filter == 3:
                    # Games that team has won
                    victory_subq = GameSide.select(GameSide.game).join(Game).where(
                        (GameSide.team == team_filter[0]) & (GameSide.id == Game.winner)
                    )
                elif status_filter == 4:
                    # Games that team has lost
                    victory_subq = GameSide.select(GameSide.game).join(Game).where(
                        (GameSide.team == team_filter[0]) & (GameSide.id != Game.winner)
                    )

        game = Game.select().where(
            (
                Game.id.in_(team_subq)
            ) & (
                Game.id.in_(player_subq)
            ) & (
                Game.id.in_(title_subq)
            ) & (
                Game.is_completed.in_(completed_filter)
            ) & (
                Game.is_confirmed.in_(confirmed_filter)
            ) & (
                Game.id.in_(victory_subq)
            ) & (
                Game.id.in_(guild_filter)
            )
        ).order_by(-Game.date).prefetch(GameSide, Team, Lineup, Player)

        return game

    def recalculate_elo_since(timestamp):
        games = Game.select().where(
            (Game.is_completed == 1) & (Game.completed_ts >= timestamp) & (Game.winner.is_null(False))
        ).prefetch(GameSide, Lineup)

        for g in games:
            g.reverse_elo_changes()
            g.is_completed = 0  # To have correct completed game counts for new ELO calculations
            g.save()

        for g in games:
            full_game = Game.load_full_game(game_id=g.id)
            print(f'Calculating ELO for game {g.id}')
            full_game.declare_winner(winning_side=full_game.winner, confirm=True)

    def recalculate_all_elo():
        # Reset all ELOs to 1000, reset completed game counts, and re-run Game.declare_winner() on all qualifying games

        # This could be made less-DB intensive by:
        # 1) limiting reset to one guild ID
        # 2) have a way to only affect games that ended after a deleted game (if thats why recalc is occuring)

        logger.warn('Resetting and recalculating all ELO')

        with db.atomic():
            Player.update(elo=1000).execute()
            Team.update(elo=1000).execute()
            DiscordMember.update(elo=1000).execute()
            Squad.update(elo=1000).execute()

            Game.update(is_completed=0).where(
                (Game.is_confirmed == 1) & (Game.winner.is_null(False))
            ).execute()  # Resets completed game counts for players/squads/team ELO bonuses

            games = Game.select().where(
                (Game.is_completed == 0) & (Game.is_confirmed == 1) & (Game.winner.is_null(False))
            ).order_by(Game.completed_ts)

            for game in games:
                full_game = Game.load_full_game(game_id=game.id)
                print(f'Calculating ELO for game {game.id}')
                full_game.declare_winner(winning_side=full_game.winner, confirm=True)

    def first_open_side(self):
        for side in self.gamesides:
            if len(side.lineup) < side.size:
                return side
        return None

    def get_side(self, lookup):
        # lookup can be a side number/position (integer) or side name
        # returns (GameSide, bool) where bool==True if side has space to add a player
        try:
            side_num = int(lookup)
            side_name = None
        except ValueError:
            side_num = None
            side_name = lookup

        for side in self.gamesides:
            print(side)
            if side_num and side.position == side_num:
                return (side, bool(len(side.lineup) < side.size))
            if side_name and side.name and len(side_name) > 2 and side_name.upper() in side.sidename.upper():
                return (side, bool(len(side.lineup) < side.size))

        return None, False

    def subq_open_games_with_capacity(guild_id: int = None):
        # All games that have open capacity
        # not restricted by expiration

        # Subq: MatchSides with openings
        subq = GameSide.select(GameSide.id).join(Lineup, JOIN.LEFT_OUTER).group_by(GameSide.id, GameSide.size).having(
            fn.COUNT(Lineup.id) < GameSide.size)

        if guild_id:
            q = GameSide.select(GameSide.game).join(Game).where(
                (GameSide.id.in_(subq)) & (GameSide.game.guild_id == guild_id) & (GameSide.game.is_pending == 1)
            ).group_by(GameSide.game).order_by(GameSide.game)

        else:
            q = GameSide.select(GameSide.game).join(Game).where(
                (GameSide.id.in_(subq)) & (GameSide.game.is_pending == 1)
            ).group_by(GameSide.game).order_by(GameSide.game)

        return q

    def waiting_to_start(guild_id: int, host_discord_id: int = None):
        # Open games that are full and still pending

        if host_discord_id:
            q = Game.select().join(Player).join(DiscordMember).where(
                (Game.id.not_in(Game.subq_open_games_with_capacity())) &
                (Game.host.discord_member.discord_id == host_discord_id) &
                (Game.is_pending == 1) &
                (Game.guild_id == guild_id)
            )
        else:
            q = Game.select().where(
                (Game.id.not_in(Game.subq_open_games_with_capacity())) & (Game.is_pending == 1) & (Game.guild_id == guild_id)
            )
        return q

    def purge_expired_games():

        # Full matches that expired more than 3 days ago (ie. host has 3 days to start match before it vanishes)
        purge_deadline = (datetime.datetime.now() + datetime.timedelta(days=-3))

        delete_query = Game.delete().where(
            (Game.expiration < purge_deadline) & (Game.is_pending == 1)
        )

        # Expired matches that never became full
        delete_query2 = Game.delete().where(
            (Game.expiration < datetime.datetime.now()) & (Game.id.in_(Game.subq_open_games_with_capacity())) & (Game.is_pending == 1)
        )

        logger.info(f'purge_expired_games #1: Purged {delete_query.execute()}  games.')
        logger.info(f'purge_expired_games #2: Purged {delete_query2.execute()}  games.')


class Squad(BaseModel):
    elo = SmallIntegerField(default=1000)
    guild_id = BitField(unique=False, null=False)

    def upsert(player_list, guild_id: int):

        squads = Squad.get_matching_squad(player_list)

        if len(squads) == 0:
            # Insert new squad based on this combination of players
            sq = Squad.create(guild_id=guild_id)
            for p in player_list:
                SquadMember.create(player=p, squad=sq)
            return sq

        return squads[0]

    def completed_game_count(self):

        num_games = GameSide.select().join(Game).where(
            (Game.is_completed == 1) & (GameSide.squad == self)
        ).count()

        return num_games

    def change_elo_after_game(self, chance_of_winning: float, is_winner: bool):
        print(f'Squad CoW: {chance_of_winning}')
        if self.completed_game_count() < 6:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        if is_winner is True:
            elo_delta = int(round((max_elo_delta * (1 - chance_of_winning)), 0))
        else:
            elo_delta = int(round((max_elo_delta * (0 - chance_of_winning)), 0))

        self.elo = int(self.elo + elo_delta)
        self.save()

        return elo_delta

    def subq_squads_by_size(min_size: int=2, exact=False):

        if exact:
            # Squads with exactly min_size number of members
            return SquadMember.select(SquadMember.squad).group_by(
                SquadMember.squad
            ).having(fn.COUNT('*') == min_size)

        # Squads with at least min_size number of members
        return SquadMember.select(SquadMember.squad).group_by(
            SquadMember.squad
        ).having(fn.COUNT('*') >= min_size)

    def subq_squads_with_completed_games(min_games: int=1):
        # Defaults to squads who have completed more than 0 games

        if min_games <= 0:
            # Squads who at least have one in progress game
            return SquadGame.select(SquadGame.squad).join(Game).where(Game.is_pending == 0).group_by(
                SquadGame.squad
            ).having(fn.COUNT('*') >= min_games)

        return SquadGame.select(SquadGame.squad).join(Game).where(Game.is_completed == 1).group_by(
            SquadGame.squad
        ).having(fn.COUNT('*') >= min_games)

    def leaderboard_rank(self, date_cutoff):

        query = Squad.leaderboard(date_cutoff=date_cutoff, guild_id=self.guild_id)

        squad_found = False
        for counter, s in enumerate(query.tuples()):
            if s[0] == self.id:
                squad_found = True
                break

        rank = counter + 1 if squad_found else None
        return (rank, query.count())

    def leaderboard(date_cutoff, guild_id: int):

        num_squads = Squad.select().where(Squad.guild_id == guild_id).count()
        if num_squads < 15:
            min_games = 0
        elif num_squads < 25:
            min_games = 1
        else:
            min_games = 2

        q = Squad.select().join(GameSide).join(Game).where(
            (
                Squad.id.in_(Squad.subq_squads_with_completed_games(min_games=min_games))
            ) & (Squad.guild_id == guild_id) & (Game.date > date_cutoff)
        ).order_by(-Squad.elo).group_by(Squad).prefetch(SquadMember, Player)

        return q

    def get_matching_squad(player_list):
        # Takes [List, of, Player, Records] (not names)
        # Returns squad with exactly the same participating players. See https://stackoverflow.com/q/52010522/1281743
        query = Squad.select().join(SquadMember).group_by(Squad.id).having(
            (fn.SUM(SquadMember.player.in_(player_list).cast('integer')) == len(player_list)) & (fn.SUM(SquadMember.player.not_in(player_list).cast('integer')) == 0)
        )

        return query

    def get_all_matching_squads(player_list):
        # Takes [List, of, Player, Records] (not names)
        # Returns all squads containing players in player list. Used to look up a squad by partial or complete membership

        # Limited to squads with at least 2 members and at least 1 completed game
        query = Squad.select().join(SquadMember).where(
            (Squad.id.in_(Squad.subq_squads_by_size(min_size=2))) & (Squad.id.in_(Squad.subq_squads_with_completed_games(min_games=1)))
        ).group_by(Squad.id).having(
            (fn.SUM(SquadMember.player.in_(player_list).cast('integer')) == len(player_list))
        )

        return query

    def get_record(self):

        wins = GameSide.select(GameSide.id).join(Game).where(
            (Game.is_completed == 1) & (GameSide.squad == self) & (GameSide.id == Game.winner)
        ).count()

        losses = GameSide.select(GameSide.id).join(Game).where(
            (Game.is_completed == 1) & (GameSide.squad == self) & (GameSide.id != Game.winner)
        ).count()

        return (wins, losses)

    def get_members(self):
        members = [member.player for member in self.squadmembers]
        return members

    def get_names(self):
        member_names = [member.player.name for member in self.squadmembers]
        return member_names


class SquadMember(BaseModel):
    player = ForeignKeyField(Player, null=False, on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=False, backref='squadmembers', on_delete='CASCADE')


class GameSide(BaseModel):
    game = ForeignKeyField(Game, null=False, backref='gamesides', on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=True, backref='gamesides', on_delete='CASCADE')
    team = ForeignKeyField(Team, null=True, backref='gamesides', on_delete='RESTRICT')
    elo_change_squad = SmallIntegerField(default=0)
    elo_change_team = SmallIntegerField(default=0)
    team_chan = BitField(default=None, null=True)
    sidename = TextField(null=True)  # for pending open games/matchmaking
    size = SmallIntegerField(null=False, default=1)
    position = SmallIntegerField(null=False, unique=False, default=1)

    def calc_win_chance(my_side_elo: int, opponent_elo: int):
        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - my_side_elo) / 400.0))), 3)
        return chance_of_winning

    def elo_strings(self):
        # Returns a tuple of strings for team ELO and squad ELO display. ie:
        # ('1200 +30', '1300')

        if self.team:
            team_elo_str = str(self.elo_change_team) if self.elo_change_team != 0 else ''
            if self.elo_change_team > 0:
                team_elo_str = '+' + team_elo_str
            team_elo_str = f'({self.team.elo} {team_elo_str})'
        else:
            team_elo_str = None

        if self.squad:
            squad_elo_str = str(self.elo_change_squad) if self.elo_change_squad != 0 else ''
            if self.elo_change_squad > 0:
                squad_elo_str = '+' + squad_elo_str
            if squad_elo_str:
                squad_elo_str = '(' + squad_elo_str + ')'

            squad_elo_str = f'{self.squad.elo} {squad_elo_str}'
        else:
            squad_elo_str = None

        return (team_elo_str, squad_elo_str)

    def average_elo(self):
        elo_list = [l.player.elo for l in self.lineup]
        return int(round(sum(elo_list) / len(elo_list)))

    def adjusted_elo(self, missing_players: int, own_elo: int, opponent_elos: int):
        # If teams have imbalanced size, adjust win% based on a
        # function of the team's elos involved, e.g.
        # 1v2  [1400] vs [1100, 1100] adjusts to represent 50% win
        # (compared to 58.8% for 1v1v1 for the 1400 player)
        handicap = 300  # the elo difference for a 50% 1v2 chance
        handicap_elo = handicap * 2 + max(own_elo - opponent_elos - handicap, 0)
        size = len(self.lineup)

        # "fill up" missing players with placeholder handicapped elos
        missing_player_elo = own_elo - handicap_elo
        return int(round((own_elo * size + missing_player_elo * missing_players) / (size + missing_players)))

    def name(self):
        if len(self.lineup) == 1:
            # 1-player side
            if len(self.game.lineup) > 10:
                return self.lineup[0].player.discord_member.name[:10]
            elif len(self.game.lineup) > 6:
                return self.lineup[0].player.discord_member.name[:20]
            else:
                return self.lineup[0].player.name[:30]
        else:
            # Team game
            return self.team.name

    def roster(self):
        # Returns list of tuples [(player, elo string (1000 +50), :tribe_emoji:)]
        players = []

        for l in self.lineup:
            elo_str = str(l.elo_change_player) if l.elo_change_player != 0 else ''
            if l.elo_change_player > 0:
                elo_str = '+' + elo_str
            players.append(
                (l.player, f'{l.player.elo} {elo_str}', l.emoji_str())
            )

        return players


class Lineup(BaseModel):
    tribe = ForeignKeyField(TribeFlair, null=True, on_delete='SET NULL')
    game = ForeignKeyField(Game, null=False, backref='lineup', on_delete='CASCADE')
    gameside = ForeignKeyField(GameSide, null=False, backref='lineup', on_delete='CASCADE')
    player = ForeignKeyField(Player, null=False, backref='lineup', on_delete='CASCADE')
    elo_change_player = SmallIntegerField(default=0)
    elo_change_discordmember = SmallIntegerField(default=0)

    def change_elo_after_game(self, chance_of_winning: float, is_winner: bool):
        # Average(Away Side Elo) is compared to Average(Home_Side_Elo) for calculation - ie all members on a side will have the same elo_delta
        # Team A: p1 900 elo, p2 1000 elo = 950 average
        # Team B: p1 1000 elo, p2 1200 elo = 1100 average
        # ELO is compared 950 vs 1100 and all players treated equally

        print(f'Player CoW: {chance_of_winning}')
        num_games = self.player.completed_game_count()

        if num_games < 6:
            max_elo_delta = 75
        elif num_games < 11:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        if is_winner is True:
            elo_delta = int(round((max_elo_delta * (1 - chance_of_winning)), 0))
        else:
            elo_delta = int(round((max_elo_delta * (0 - chance_of_winning)), 0))

        elo_boost = .60 * ((1200 - max(min(self.player.elo, 1200), 900)) / 300)  # 60% boost to delta at elo 900, gradually shifts to 0% boost at 1200 ELO
        elo_bonus = int(abs(elo_delta) * elo_boost)
        elo_delta += elo_bonus

        # print(f'Player chance of winning: {chance_of_winning} opponent elo:{opponent_elo} my_side_elo: {my_side_elo},'
        # f'elo_delta {elo_delta}, current_player_elo {self.player.elo}, new_player_elo {int(self.player.elo + elo_delta)}')

        self.player.elo = int(self.player.elo + elo_delta)
        self.elo_change_player = elo_delta
        self.player.save()
        self.save()

    def emoji_str(self):

        if self.tribe and self.tribe.emoji:
            return self.tribe.emoji
        else:
            return ''


class Match(BaseModel):
    host = ForeignKeyField(Player, null=False, backref='match', on_delete='RESTRICT')
    expiration = DateTimeField(null=False, default=tomorrow)
    notes = TextField(null=True)
    game = ForeignKeyField(Game, null=True, backref='match', on_delete='SET NULL')
    guild_id = BitField(unique=False, null=False)
    is_started = BooleanField(default=False)  # game = None and is_started = True if related game gets deleted

    def is_hosted_by(self, discord_id: int):
        return self.host.discord_member.discord_id == discord_id, self.host

    def player(self, player: Player = None, discord_id: int = None):
        # return match.matchplayer based on either Player object or discord_id. else None

        for matchplayer in self.matchplayers:
            if player and matchplayer.player == player:
                return matchplayer
            if discord_id and matchplayer.player.discord_member.discord_id == discord_id:
                return matchplayer
        return None

    def draft_order(self):
        # Returns list of tuples, in order of recommended draft order list:
        # [(Side #, Side Name, Player 1), ... ]

        players, capacity = self.capacity()
        if players < capacity:
            raise exceptions.CheckFailedError('This match is not full')

        sides = MatchSide.select().where(
            (MatchSide.match == self)
        ).order_by(MatchSide.position).prefetch(MatchPlayer, Player)

        picks = []
        side_objs = [{'side': s, 'pick_score': 0, 'size': s.size} for s in sides]
        num_tribes = sum([s.size for s in sides])

        for pick in range(num_tribes):
            picking_team = None
            lowest_score = 99
            # for team in teams:
            for side_obj in side_objs:
                # Find side with lowest pick_score
                if side_obj['pick_score'] <= lowest_score and lowest_score > 0 and side_obj['size'] > 0:
                    picking_team = side_obj
                    lowest_score = side_obj['pick_score']

            picking_team['pick_score'] = picking_team['pick_score'] + num_tribes
            picking_team['size'] = picking_team['size'] - 1
            num_tribes = num_tribes - 1
            picks.append(
                (picking_team['side'].position, picking_team['side'].name, picking_team['side'].sideplayers.pop(0))
            )

        return picks

    def size_string(self):
        if max(s.size for s in self.sides) and len(self.sides) > 2:
            return 'FFA'
        else:
            return 'v'.join(str(s.size) for s in self.sides)

    def capacity(self):
        return (len(self.matchplayers), sum(s.size for s in self.sides))

    def embed(self, ctx):
        embed = discord.Embed(title=f'Match **M{self.id}**\n{self.size_string()} *hosted by* {self.host.name}')
        notes_str = self.notes if self.notes else "\u200b"
        content_str = None

        if self.expiration < datetime.datetime.now():
            expiration_str = f'*Expired*'
            status_str = 'Expired'
        else:
            expiration_str = f'{int((self.expiration - datetime.datetime.now()).total_seconds() / 3600.0)} hours'
            status_str = f'Open - `{ctx.prefix}join M{self.id}`'

        players, capacity = self.capacity()
        if players >= capacity:
            if self.is_started:
                status_str = f'Started - Game # {self.game.id} **{self.game.name}**' if self.game else 'Started'
            else:
                if players > 2:
                    draft_order = ['\n__**Balanced Draft Order**__']
                    for draft in self.draft_order():
                        draft_order.append(f'__Side {draft[1] if draft[1] else draft[0]}__:  {draft[2].player.name}')
                    draft_order_str = '\n'.join(draft_order)
                else:
                    draft_order_str = ''
                content_str = (f'This match is now full and the host should create the game in Polytopia and start it with `{ctx.prefix}startmatch M{self.id} Name of Game`'
                        f'{draft_order_str}')
                status_str = 'Full - Waiting to start'

        embed.add_field(name='Status', value=status_str, inline=True)
        embed.add_field(name='Expires in', value=f'{expiration_str}', inline=True)
        embed.add_field(name='Notes', value=notes_str, inline=False)
        embed.add_field(name='\u200b', value='\u200b', inline=False)

        for side in self.sides:
            # TODO: this wont print in side.position order if they have been saved() in odd order after creation
            side_name = ': **' + side.name + '**' if side.name else ''
            side_capacity = side.capacity()
            capacity += side_capacity[1]
            player_list = []
            for matchplayer in side.sorted_players():
                players += 1
                player_list.append(f'**{matchplayer.player.name}** ({matchplayer.player.elo})\n{matchplayer.player.discord_member.polytopia_id}')
            player_str = '\u200b' if not player_list else '\n'.join(player_list)
            embed.add_field(name=f'__Side {side.position}__{side_name} *({side_capacity[0]}/{side_capacity[1]})*', value=player_str)

        return embed, content_str

    def first_open_side(self):
        for side in self.sides:
            if len(side.sideplayers) < side.size:
                return side
        return None

    def get_side(self, lookup):
        # lookup can be a side number/position (integer) or side name
        # returns (MatchSide, bool) where bool==True if side has space to add a player
        try:
            side_num = int(lookup)
            side_name = None
        except ValueError:
            side_num = None
            side_name = lookup

        for side in self.sides:
            if side_num and side.position == side_num:
                return (side, bool(len(side.sideplayers) < side.size))
            if side_name and side.name and len(side_name) > 2 and side_name.upper() in side.name.upper():
                return (side, bool(len(side.sideplayers) < side.size))

        return None, False

    def purge_expired_matches():

        # Full matches that expired more than 3 days ago (ie. host has 3 days to start match before it vanishes)
        purge_deadline = (datetime.datetime.now() + datetime.timedelta(days=-3))

        delete_query = Match.delete().where(
            (Match.expiration < purge_deadline) & (Match.game.is_null(True))
        )

        # Expired matches that never became full
        delete_query2 = Match.delete().where(
            (Match.expiration < datetime.datetime.now()) & (Match.id.in_(Match.subq_open_matches()))
        )

        logger.debug(f'purge_expired_matches #1: Purged {delete_query.execute()}  matches.')
        logger.debug(f'purge_expired_matches #2: Purged {delete_query2.execute()}  matches.')

    def subq_open_matches(guild_id: int = None):
        # All Matches that have open capacity
        # not restricted by expiration

        # Subq: MatchSides with openings
        subq = MatchSide.select(MatchSide.id).join(MatchPlayer, JOIN.LEFT_OUTER).group_by(MatchSide.id, MatchSide.size).having(
            fn.COUNT(MatchPlayer.id) < MatchSide.size)

        if guild_id:
            q = MatchSide.select(MatchSide.match).join(Match).where(
                (MatchSide.id.in_(subq)) & (MatchSide.match.guild_id == guild_id)
            ).group_by(MatchSide.match).order_by(MatchSide.match)

        else:
            q = MatchSide.select(MatchSide.match).join(Match).where(
                (MatchSide.id.in_(subq))
            ).group_by(MatchSide.match).order_by(MatchSide.match)

        return q

    def waiting_to_start(guild_id: int, host_discord_id: int = None):
        # Could be rolled in to Match.search but that method would need to be changed to allow expired matches in results

        if host_discord_id:
            q = Match.select().join(Player).join(DiscordMember).where(
                (Match.id.not_in(Match.subq_open_matches())) &
                (Match.host.discord_member.discord_id == host_discord_id) &
                (Match.is_started == 0) &
                (Match.guild_id == guild_id)
            )
        else:
            q = Match.select().where(
                (Match.id.not_in(Match.subq_open_matches())) & (Match.is_started == 0) & (Match.guild_id == guild_id)
            )
        return q

    def search(guild_id: int, player: Player = None, search: str = None, status: int = None):
        # Status: 1 - not full, 2 - full
        # Returns matches where player is a participant/host, OR search is found in match notes OR search is found in match side names
        player_q, search_q = [], []

        if player:
            psubq = MatchPlayer.select(MatchPlayer.match).join(Match).where(
                (MatchPlayer.player == player)
            ).group_by(MatchPlayer.match)

            player_q = Match.select(Match.id).where(
                (Match.host == player) | (Match.id.in_(psubq))
            )

        if search:
            ssubq = MatchSide.select(MatchSide.match).join(Match).where(
                (MatchSide.name.contains(search))
            ).group_by(MatchSide.match)

            search_q = Match.select(Match.id).where(
                (Match.notes.contains(search)) | (Match.id.in_(ssubq))
            )

        if not player and not search:
            search_q = Match.select(Match.id)

        if status == 1:
            status_filter = Match.select(Match.id).where(Match.id.in_(Match.subq_open_matches()))
        elif status == 2:
            status_filter = Match.select(Match.id).where(Match.id.not_in(Match.subq_open_matches()))
        else:
            status_filter = Match.select(Match.id)

        return Match.select().where(
            ((Match.id.in_(player_q)) | (Match.id.in_(search_q))) &
            (Match.expiration > datetime.datetime.now()) &
            (Match.guild_id == guild_id) &
            (Match.id.in_(status_filter)) &
            (Match.game.is_null(True))
        ).order_by(-Match.id).prefetch(MatchSide)


class MatchSide(BaseModel):
    match = ForeignKeyField(Match, null=False, backref='sides', on_delete='CASCADE')
    name = TextField(null=True)
    size = SmallIntegerField(null=False, default=1)
    position = SmallIntegerField(null=False, unique=False, default=1)

    class Meta:
        indexes = ((('match', 'position'), True),)   # Trailing comma is required

    def capacity(self):
        return (len(self.sideplayers), self.size)

    def sorted_players(self):
        q = MatchPlayer.select(MatchPlayer, Player, DiscordMember).join(Player).join(DiscordMember).where(
            MatchPlayer.side == self
        ).order_by(MatchPlayer.position)
        return q


class MatchPlayer(BaseModel):
    side = ForeignKeyField(MatchSide, null=False, backref='sideplayers', on_delete='CASCADE')
    match = ForeignKeyField(Match, null=False, backref='matchplayers', on_delete='CASCADE')
    player = ForeignKeyField(Player, null=False, backref='matches', on_delete='CASCADE')
    position = SmallIntegerField(null=False, unique=False, default=0)


with db:
    db.create_tables([Team, DiscordMember, Game, Player, Tribe, Squad, GameSide, SquadMember, Lineup, TribeFlair])
    # Only creates missing tables so should be safe to run each time
    try:
        # Creates deferred FK http://docs.peewee-orm.com/en/latest/peewee/models.html#circular-foreign-key-dependencies
        Game._schema.create_foreign_key(Game.winner)
    except ProgrammingError:
        pass
        # Will throw this exception if the foreign key has already been created
