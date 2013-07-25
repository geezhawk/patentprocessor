import os
import re
import ConfigParser

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from collections import defaultdict
from collections import Counter
from schema import *


def get_config(localfile="config.ini", default_file=True):
    """
    This grabs a configuration file and converts it into
    a dictionary.

    The default filename is called config.ini
    First we load the global file, then we load a local file
    """
    if default_file:
        openfile = "{0}/config.ini".format(os.path.dirname(os.path.realpath(__file__)))
    else:
        openfile = localfile
    config = defaultdict(dict)
    if os.path.isfile(openfile):
        cfg = ConfigParser.ConfigParser()
        cfg.read(openfile)
        for s in cfg.sections():
            for k, v in cfg.items(s):
                dec = re.compile('\d+(\.\d+)?')
                if v in ("True", "False") or v.isdigit() or dec.match(v):
                    v = eval(v)
                config[s][k] = v

    # this enables us to load a local file
    if default_file:
        newconfig = get_config(localfile, default_file=False)
        for section in newconfig:
            for item in newconfig[section]:
                config[section][item] = newconfig[section][item]

    return config


def fetch_session(db=None):
    """
    Read from config.ini file and load appropriate database
    """
    config = get_config()
    echo = config.get('global').get('echo')
    if not db:
        db = config.get('global').get('database')
    if db[:6] == "sqlite":
        sqlite_db_path = os.path.join(
            config.get(db).get('path'),
            config.get(db).get('database'))
        engine = create_engine('sqlite:///{0}'.format(sqlite_db_path), echo=echo)
    else:
        engine = create_engine('mysql+mysqldb://{0}:{1}@{2}/{3}?charset=utf8'.format(
            config.get(db).get('user'),
            config.get(db).get('password'),
            config.get(db).get('host'),
            config.get(db).get('database')), echo=echo)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    return session

session = fetch_session()


def match(objects=[], override={}):
    """
    Pass in several objects and make them equal
    Override is specified if there is a desire to override
    certain fields

    TODO: Add stuff to have a flag so it doesn't search
    TODO: Add stuff to default to certain variables
    TODO: Add better linkage of inventor/assignee > location
    """
    if type(objects).__name__ not in ('list', 'tuple'):
        objects = [objects]
    freq = defaultdict(Counter)
    param = {}
    all_objects = []
    all_objects.extend(objects)
    clean_objects = []

    # we extend our objects and determine the
    # previously associated items
    for obj in objects:
        clean = obj.__clean__
        # keep track of all the "clean" objects
        if clean:
            if clean not in clean_objects:
                clean_objects.append(clean)
            # add the "raw" objects as we want to
            # iterate these items
            for o in clean.__raw__:
                if o not in all_objects:
                    all_objects.append(o)

    # this helps us determine items to summarize
    # ques: how do we indicate most recent?
    #   like for people and their locations? hrm..
    for obj in all_objects:
        for k, v in obj.summarize.iteritems():
            if v not in (None, ""):
                freq[k][v] += 1
        if "id" not in param:
            param["id"] = obj.uuid
        param["id"] = min(param["id"], obj.uuid)

    # create parameters based on most frequent
    for k in freq:
        param[k] = freq[k].most_common(1)[0][0]
    param.update(override)

    # remove all clean objects
    for obj in clean_objects:
        session.delete(obj)
    session.commit()  # commit necessary

    relobj = objects[0].__related__(**param)
    # associate the data into the related object

    for obj in all_objects:
        relobj.__raw__.append(obj)
        if type(relobj.__many__).__name__ in ("dict"):
            # if it is a dictionary type, iterate and add
            for key in relobj.__many__.keys():
                if type(obj.__single__[key]).__name__ in ('list', 'tuple'):
                    relobj.__many__[key].extend(set(obj.__single__[key]) - set(relobj.__many__[key]))
                elif obj.__single__[key] not in relobj.__many__[key]:
                    relobj.__many__[key].append(obj.__single__[key])
        else:
            if obj.__single__ and obj.__single__ not in relobj.__many__:
                relobj.__many__.append(obj.__single__)
    session.merge(relobj)
    session.commit()


def add(obj, override=True, temp=False):
    """
    PatentGrant Object converting to tables via SQLAlchemy
    Necessary to convert dates to datetime because of SQLite (OK on MySQL)

    Case Sensitivity and Table Reflection
spr0us    MySQL has inconsistent support for case-sensitive identifier names,
    basing support on specific details of the underlying operating system.
    However, it has been observed that no matter what case sensitivity
    behavior is present, the names of tables in foreign key declarations
    are always received from the database as all-lower case, making it
    impossible to accurately reflect a schema where inter-related tables
    use mixed-case identifier names.

    Therefore it is strongly advised that table names be declared as all
    lower case both within SQLAlchemy as well as on the MySQL database
    itself, especially if database reflection features are to be used.
    """

    # if a patent exists, remove it so we can replace it
    pat_query = session.query(Patent).filter(Patent.number == obj.patent)
    if pat_query.count():
        if override:
            session.delete(pat_query.one())
        else:
            return
    if len(obj.pat["number"]) < 3:
        return

    #add
    # lots of abstracts seem to be missing. why?

    pat = Patent(**obj.pat)
    pat.application = Application(**obj.app)

    #+asg
    for asg, loc in obj.assignee_list:
        asg = RawAssignee(**asg)
        loc = RawLocation(**loc)
        session.merge(loc)
        asg.rawlocation = loc
        pat.rawassignees.append(asg)

    #+inv
    for inv, loc in obj.inventor_list:
        inv = RawInventor(**inv)
        loc = RawLocation(**loc)
        session.merge(loc)
        inv.rawlocation = loc
        pat.rawinventors.append(inv)

    #+law
    for law in obj.lawyer_list:
        law = RawLawyer(**law)
        pat.rawlawyers.append(law)

    #+usreldoc
    for usr in obj.us_relation_list:
        usr = USRelDoc(**usr)
        pat.usreldocs.append(usr)

    #+classes
    for uspc, mc, sc in obj.us_classifications:
        uspc = USPC(**uspc)
        mc = MainClass(**mc)
        sc = SubClass(**sc)
        session.merge(mc)
        session.merge(sc)
        uspc.mainclass = mc
        uspc.subclass = sc
        pat.classes.append(uspc)

    #+ipcr
    for ipc in obj.ipcr_classifications:
        ipc = IPCR(**ipc)
        pat.ipcrs.append(ipc)

    # citations are huge. this dumps them to
    # a temporary database which we can use for later
    if temp:
        cits, refs = obj.citation_list
        for cit in cits:
            cit["patent_id"] = obj.pat["number"]
            cit = TempCitation(**cit)
            session.add(cit)
        for ref in refs:
            ref["patent_id"] = obj.pat["number"]
            ref = TempOtherReference(**ref)
            session.add(ref)
    else:
        cits, refs = obj.citation_list
        for cit in cits:
            cit = Citation(**cit)
            pat.citations.append(cit)
        for ref in refs:
            ref = OtherReference(**ref)
            pat.otherreferences.append(ref)

    session.merge(pat)


def commit():
    try:
        session.commit()
    except Exception, e:
        session.rollback()
        print str(e)
