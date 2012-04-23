from functools import wraps

from twisted.words.protocols import irc
from twisted.internet import reactor, protocol
from twisted.internet.ssl import ClientContextFactory
from twisted.python import log

from ..pluginbase import BotPlugin
from ..transport import Event
from ..command import CommandPluginSuperclass

def decode_utf8_or_88591(s):
    try:
        return s.decode("UTF-8")
    except UnicodeDecodeError:
        return s.decode("CP1252", 'replace')

def decode_args(func):
    """Decorator that decodes each bytestring arg of func with UTF-8 into a
    unicode string

    """
    @wraps(func)
    def newfunc(*args, **kwargs):
        newargs = [decode_utf8_or_88591(x) if isinstance(x, str) else x for x in args]
        newkwargs = {}
        for key in kwargs:
            if isinstance(kwargs[key], str):
                newkwargs[key] = decode_utf8_or_88591(kwargs[key])
            else:
                newkwargs[key] = kwargs[key]
        return func(*newargs, **newkwargs)
    return newfunc

def encode_args(func):
    """Opposite of decode_args, decorates outgoing functions"""
    @wraps(func)
    def newfunc(*args, **kwargs):
        newargs = [x.encode("UTF-8") if isinstance(x, unicode) else x for x in args]
        newkwargs = {}
        for key in kwargs:
            if isinstance(kwargs[key], unicode):
                newkwargs[key] = kwargs[key].encode("UTF-8")
            else:
                newkwargs[key] = kwargs[key]
        return func(*newargs, **newkwargs)
    return newfunc

class IRCBot(irc.IRCClient):

    def __init__(self, *args, **kwargs):

        # Make sure no unicode strings leak out
        outgoing_funcs = [
                "join",
                "leave",
                "kick",
                "invite",
                "topic",
                "mode",
                "say",
                "msg",
                "notice",
                "away",
                "back",
                "whois",
                "setNick",
                "quit",
                ]
        for funcname in outgoing_funcs:
            decorated_func = encode_args(getattr(self, funcname))
            setattr(self, funcname, decorated_func)


    ### ALL METHODS BELOW ARE OVERRIDDEN METHODS OF irc.IRCClient (or ancestors)
    ### AND ARE CALLED AUTOMATICALLY UPON THE APPROPRIATE EVENTS

    def connectionMade(self):
        """This is called by Twisted once the connection has been made, and has
        access to self.factory. This is where we set up callbacks for actions
        we can perform

        """
        # Can't use super() because twisted doesn't use new-style classes
        irc.IRCClient.connectionMade(self)
        self.factory.client = self

        log.msg("Connection made")

        # Join the configured channels
        for chan in self.factory.config['channels']:
            self.join(chan)

    def connectionLost(self, reason):
        """The connection is down and this object is about to be destroyed,
        unhook our event listeners
        
        """
        self.factory.client = None
        irc.IRCClient.connectionLost(self, reason)

        log.msg("Connection lost")

    ### The following are things that happen to us

    @decode_args
    def joined(self, channel):
        """We have joined a channel"""
        log.msg("Joined channel %s" % channel)
        self.factory.broadcast_message("irc.on_join", channel=channel)

        if channel not in self.factory.config['channels']:
            self.factory.config['channels'].append(channel)
            self.factory.pluginboss.save()

    @decode_args
    def left(self, channel):
        """We have left a channel"""
        self.factory.broadcast_message("irc.on_part", channel=channel)

        if channel in self.factory.config['channels']:
            self.factory.config['channels'].remove(channel)
            self.factory.pluginboss.save()

    ### Things we see other users doing or observe about the channel

    @decode_args
    def privmsg(self, user, channel, message):
        """Someone sent us a private message or we received a channel
        message.
        
        """
        self.factory.broadcast_message("irc.on_privmsg",
                user=user, channel=channel, message=message)

    @decode_args
    def noticed(self, user, channel, message):
        """Received a notice. This is like a privmsg, but distinct."""
        self.factory.broadcast_message("irc.on_notice",
                user=user, channel=channel, message=message)

    @decode_args
    def modeChanged(self, user, channel, set, modes, args):
        """A mode has changed on a user or a channel.

        user is who instigated the change

        channel is the channel where the mode changed.

        set is true if the mode is being added, false if it is being removed.

        modes is the mode or modes which are being changed

        args is a tuple with any additional info required for the mode
        """
        self.factory.broadcast_message("irc.on_mode_change",
                user=user, chan=channel, set=set, modes=modes, args=args)

    @decode_args
    def userJoined(self, user, channel):
        self.factory.broadcast_message("irc.on_user_joined",
                user=user, channel=channel)

    @decode_args
    def userLeft(self, user, channel):
        self.factory.broadcast_message("irc.on_user_part",
                user=user, channel=channel)

    @decode_args
    def userQuit(self, user, message):
        self.factory.broadcast_message("irc.on_user_quit",
                user=user, message=message)

    @decode_args
    def userKicked(self, kickee, channel, kicker, message):
        self.factory.broadcast_message("irc.on_user_kick",
                kickee=kickee, channel=channel, kicker=kicker, message=message)

    @decode_args
    def action(self, user, channel, data):
        """User performs an action on the channel"""
        self.factory.broadcast_message("irc.on_action",
                user=user, channel=channel, data=data)

    @decode_args
    def topicUpdated(self, user, channel, newtopic):
        self.factory.broadcast_message("irc.on_topic_updated",
                user=user, channel=channel, newtopic=newtopic)

    @decode_args
    def userRenamed(self, oldnick, newnick):
        self.factory.broadcast_message("irc.on_nick_change",
                oldnick=oldnick, newnick=newnick)

    @decode_args
    def irc_unknown(self, prefix, command, params):
        """This hooks into all sorts of miscelaneous things the server sends
        us, including whois replies

        """
        self.factory.broadcast_message("irc.on_unknown",
                prefix=prefix, command=command, params=params)


class IRCBotPlugin(protocol.ReconnectingClientFactory, BotPlugin):
    """Implements a bot plugin and a twisted protocol client factory.

    """
    protocol = IRCBot

    def start(self):
        self.client = None
        self.listen_for_event("irc.do_*")
        reactor.connectSSL(self.config['server'], self.config['port'], self, ClientContextFactory())

    def stop(self):
        self.stopTrying()
        # TODO Figure out how to remove this from the reactor

    def buildProtocol(self, addr):
        p = self.protocol()
        p.factory = self
        p.nickname = self.config['nick']
        return p

    def broadcast_message(self, eventname, **kwargs):
        """This method is called by the client protocol object when an event
        comes in from the network
        
        """
        event = Event(eventname, **kwargs)
        self.transport.send_event(event)

    def received_event(self, event):
        """A command received from another plugin. We must pass it on to the client

        """
        if not self.client:
            # TODO buffer the requests if the client is not currently connected
            return

        # Maps event names to (method names, arguments) that should be called
        # on the client protocol object.
        events = {
            'irc.do_join_channel':  ('join',    ('channel',)),
            'irc.do_leave_channel':  ('leave',   ('channel',)),
            'irc.do_kick':          ('kick',    ('channel', 'user', 'reason')),
            'irc.do_invite':        ('invite',  ('user', 'channel')),
            'irc.do_topic':         ('topic',   ('channel', 'topic')),
            'irc.do_mode':          ('mode',    ('chan','set','modes','limit','user','mask')),
            'irc.do_say':           ('say',     ('channel', 'message', 'length')),
            # This is just privmsg. It can send to channels or users
            'irc.do_msg':           ('msg',     ('user', 'message', 'legnth')),
            'irc.do_notice':        ('notice',  ('user', 'message')),
            'irc.do_away':          ('away',    ('away', 'message')),
            'irc.do_back':          ('back',    ()),
            'irc.do_whois':         ('whois',   ('nickname', 'server')),
            'irc.do_setnick':       ('setNick', ('nickname',)),
            'irc.do_quit':          ('quit',    ('message',)),
            }

        methodname, methodargs = events[event.eventtype]

        kwargs = {}
        for argname in methodargs:
            try:
                arg = getattr(event, argname)
                kwargs[argname] = arg
            except AttributeError:
                pass

        method = getattr(self.client, methodname)
        method(**kwargs)

class IRCController(CommandPluginSuperclass):

    def start(self):
        super(IRCController, self).start()
        
        self.install_command(r"join (?P<channel>#\w+)$",
                "irc.control",
                self.join)
        self.help_msg("join",
                "'join <channel>' Joins an IRC channel",
                permission="irc.control")

        self.install_command(r"(part|leave)( (?P<channel>#\w+))?$",
                "irc.control",
                self.part)
        self.help_msg("part",
                "'part [channel]' Leaves the current or specified IRC channel",
                permission="irc.control")

        self.install_command(r"nick (?P<newnick>[\w-]+)",
                "irc.control",
                self.nickchange)
        self.help_msg("nick",
                "'nick <newnick>' Changes the nickname of the bot",
                permission="irc.control")

        self.install_command(r"echo (?P<msg>.*)$",
                None,
                self.echo)

    def join(self, event, match):
        channel = match.groupdict()['channel']
        
        newevent = Event("irc.do_join_channel", channel=channel)
        self.transport.send_event(newevent)

        event.reply("See you in %s!" % channel)

    def part(self, event, match):
        channel = match.groupdict().get("channel", None)

        if channel:
            newevent = Event("irc.do_leave_channel", channel=channel)
            self.transport.send_event(newevent)
            event.reply("Leaving %s" % channel)
        else:

            channel = event.channel
            if not channel.startswith("#"):
                event.reply("You must let me know what channel to leave")
                return

            newevent = Event("irc.do_leave_channel", channel=channel)
            event.reply("Goodbye %s!" % channel)
            self.transport.send_event(newevent)

    def nickchange(self, event, match):
        newnick = match.groupdict()['newnick']

        if not event.channel.startswith("#"):
            event.reply("Changing nick to %s" % newnick)

        newevent = Event("irc.do_setnick", nickname=newnick)
        self.transport.send_event(newevent)

        # Also change the configuration
        self.pluginboss.config['plugin_config']['irc.IRCBotPlugin']['nick'] = newnick
        self.pluginboss.save()
        self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].reload()

    def echo(self, event, match):
        msg = match.groupdict()['msg']
        event.reply(msg)