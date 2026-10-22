"""The same exclusive print lock on POSIX and Windows, released on file close."""
import os

if os.name != 'nt':
    import fcntl
else:
    import msvcrt

    class fcntl:
        LOCK_EX = 2

        @staticmethod
        def flock(file, operation):
            if operation != fcntl.LOCK_EX:
                raise ValueError('Only exclusive locks are supported')
            fd=file.fileno()
            if os.fstat(fd).st_size==0:
                os.write(fd,b'\0')
            os.lseek(fd,0,os.SEEK_SET)
            msvcrt.locking(fd,msvcrt.LK_LOCK,1)
